#!/usr/bin/env python3
"""Adapt Kodi mirror manifests for the stable or preview channel."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import gzip
import hashlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET
import zlib

from addons_xml_generator import PREVIEW_SUFFIX, ROOT, build, load_manifests, parse_manifest, version_tuple


RELEASE_API = 'https://api.github.com/repos/xbmc/xbmc/releases/latest'
RELEASE_LIST = 'https://api.github.com/repos/xbmc/xbmc/releases?per_page=100'
RELEASE_TAG = re.compile(r'(?P<number>[0-9]+\.[0-9]+(?:\.[0-9]+)?)(?P<stage>(?:a|b|rc)[0-9]+)?-[A-Za-z0-9]+')
PREVIEW_RELEASE_MARKER = 'kodi-release: '
UPSTREAM_FILES = 'https://raw.githubusercontent.com/xbmc/xbmc'
REPO_POINT = "extension[@point='xbmc.addon.repository']"
INDEX_LIMIT = 8 * 1024 * 1024
XML_LIMIT = 48 * 1024 * 1024


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise URLError('redirect to a non-HTTPS URL')
        if req.has_header('Authorization') and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            raise URLError('authenticated redirect to another host')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit, *, headers=None):
    if urlsplit(url).scheme != 'https':
        raise ValueError(f'Expected an HTTPS URL: {url}')
    request = Request(url, headers={'User-Agent': 'Kodi-mirror-repository-sync', **(headers or {})})
    opener = build_opener(HTTPSRedirectHandler())
    for attempt in range(2):
        try:
            with opener.open(request, timeout=10) as response:
                data = response.read(limit + 1)
            break
        except HTTPError as error:
            if attempt or (error.code != 429 and not 500 <= error.code < 600):
                raise
        except (OSError, HTTPException) as error:
            if attempt:
                raise URLError(f'{url}: {error}') from error
        time.sleep(0.5)
    if len(data) > limit:
        raise ValueError(f'Response exceeds {limit} bytes')
    return data


@dataclass(frozen=True)
class Release:
    tag: str
    api: str
    series: str
    index_name: str


def release_json(url, token):
    headers = {'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        release_data = download(url, 4 * 1024 * 1024, headers=headers)
    except HTTPError as error:
        if not token or error.code not in (401, 403, 404):
            raise
        headers.pop('Authorization')
        release_data = download(url, 4 * 1024 * 1024, headers=headers)
    return json.loads(release_data)


def release_key(tag):
    match = RELEASE_TAG.fullmatch(tag)
    if match is None:
        return None
    parts = tuple(map(int, match['number'].split('.')))
    stage = match['stage']
    if stage is None:
        return parts + (0,) * (3 - len(parts)), (3, 0)
    prefix = stage.rstrip('0123456789')
    return parts + (0,) * (3 - len(parts)), (('a', 'b', 'rc').index(prefix), int(stage[len(prefix):]))


def fetch_latest_release(token=None, *, channel='stable'):
    release = release_json(RELEASE_API, token)
    tag = release.get('tag_name', '')
    stable_key = release_key(tag)
    if (release.get('draft') is not False or release.get('prerelease') is not False
            or stable_key is None or stable_key[1] != (3, 0)):
        raise ValueError(f'Unexpected stable Kodi release: {tag!r}')
    if channel == 'preview':
        candidates = []
        for entry in release_json(RELEASE_LIST, token):
            if entry.get('draft') is not False or entry.get('prerelease') is not True:
                continue
            candidate = entry.get('tag_name', '')
            key = release_key(candidate)
            if key is not None and key[0] > stable_key[0] and key[1] != (3, 0):
                candidates.append((key, candidate))
        if not candidates:
            return None
        _, tag = max(candidates)
    upstream = f'{UPSTREAM_FILES}/{quote(tag, safe="")}'
    version_txt = download(f'{upstream}/version.txt', 32 * 1024).decode('utf-8')
    api_line = re.search(r'^ADDON_API[ \t]+([^\s]+)[ \t]*\r?$', version_txt, re.MULTILINE)
    if api_line is None:
        raise ValueError(f'{tag}: ADDON_API is missing from version.txt')
    api = api_line.group(1)
    version_tuple(api)

    repo_xml = download(f'{upstream}/addons/repository.xbmc.org/addon.xml', 256 * 1024)
    dirs = ET.fromstring(repo_xml).findall(f'{REPO_POINT}/dir')
    if len(dirs) != 1:
        raise ValueError(f'{tag}: expected one upstream repository directory, got {len(dirs)}')
    datadir = dirs[0].findtext('datadir', '').rstrip('/')
    info = dirs[0].findtext('info', '')
    match = re.fullmatch(r'https://mirrors\.kodi\.tv/addons/([a-z0-9-]+)', datadir)
    if not match or info != f'{datadir}/addons.xml.gz':
        raise ValueError(f'{tag}: unsupported upstream repository layout ({datadir}, {info})')
    return Release(tag, api, match.group(1), 'addons.xml.gz')


def latest_directory(addon):
    return max(addon.findall(f'{REPO_POINT}/dir'), key=lambda d: version_tuple(d.attrib['maxversion']))


def mirror_urls(addon, release):
    previous = latest_directory(addon).findtext('datadir').rstrip('/')
    datadir = f'{previous.rsplit("/", 1)[0]}/{release.series}'
    return datadir, f'{datadir}/{release.index_name}'


@dataclass(frozen=True)
class MirrorResult:
    datadir: str
    info: str
    checksum: str | None = None
    verify: str | None = None
    error: str | None = None


def probe_mirror(datadir, info, existing_checksum=None):
    try:
        compressed = download(info, INDEX_LIMIT)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            xml = stream.read(XML_LIMIT + 1)
        if len(xml) > XML_LIMIT:
            raise ValueError('uncompressed index exceeds size limit')
        index = ET.fromstring(xml)
        if index.tag != 'addons' or not any(a.get('id') and a.get('version') for a in index.findall('addon')):
            raise ValueError('index does not contain Kodi add-ons')

        digest = hashlib.sha256(compressed).hexdigest().encode('ascii')
        mismatch = False
        sources = dict.fromkeys(filter(None, (existing_checksum, f'{info}?sha256', f'{info}.sha256')))
        for url in sources:
            try:
                checksum = download(url, 512).strip().split(maxsplit=1)
            except (OSError, ValueError):
                continue
            if checksum and re.fullmatch(rb'[0-9a-fA-F]{64}', checksum[0]):
                if checksum[0].lower() == digest:
                    return MirrorResult(datadir, info, url, 'sha256')
                mismatch = True
        if mismatch:
            raise ValueError('mirror SHA-256 does not match the downloaded index')
        if existing_checksum:
            raise ValueError('configured SHA-256 endpoint could not be verified')
        # Kodi 21+ only loads directories for which it could read a checksum.
        # Mirrors without a sidecar use the gzip itself as a legacy change marker.
        return MirrorResult(datadir, info, info)
    except (OSError, ValueError, EOFError, ET.ParseError, zlib.error) as error:
        return MirrorResult(datadir, info, error=' '.join(str(error).split()))


def adapt(addon, release, result):
    previous = latest_directory(addon)
    api = version_tuple(release.api)
    latest = version_tuple(previous.attrib['maxversion'])
    if result.error or api < latest:
        return None
    same_series = previous.findtext('datadir').rstrip('/') == result.datadir
    if same_series:
        checksum = previous.find('checksum')
        if checksum.get('verify') and not result.verify:
            return None
        if (api == latest and previous.findtext('info') == result.info
                and checksum.text == result.checksum and checksum.get('verify') == result.verify):
            return None
        entry = previous
    else:
        if api == latest:
            raise ValueError(f'{addon.attrib["id"]}: {release.api} is already assigned to another series')
        repo = addon.find(REPO_POINT)
        entry = ET.SubElement(repo, 'dir', {'minversion': release.api})
        for field in ('info', 'checksum', 'datadir', 'hashes'):
            ET.SubElement(entry, field)
        entry.find('hashes').text = 'false'

    entry.set('maxversion', release.api)
    entry.find('info').text = result.info
    checksum = entry.find('checksum')
    checksum.text = result.checksum
    if result.verify:
        checksum.set('verify', result.verify)
    entry.find('datadir').text = result.datadir
    major, minor, patch = version_tuple(addon.attrib['version'])
    addon.set('version', f'{major}.{minor}.{patch + 1}')
    ET.indent(addon, space='  ')
    return ET.tostring(addon, encoding='utf-8', xml_declaration=True) + b'\n'


def adapt_preview(stable, preview, release, result):
    if result.error:
        return None
    if preview is None:
        mirror_name = stable.get('name', stable.attrib['id'])
        addon = ET.Element('addon', {
            'id': stable.attrib['id'] + PREVIEW_SUFFIX,
            'name': mirror_name + ' (Preview)', 'version': '1.0.0',
            'provider-name': stable.get('provider-name', mirror_name),
        })
        repo = ET.SubElement(addon, 'extension', {'point': 'xbmc.addon.repository'})
        metadata = ET.SubElement(addon, 'extension', {'point': 'xbmc.addon.metadata'})
        ET.SubElement(metadata, 'summary').text = 'Kodi prerelease add-ons via ' + mirror_name
        ET.SubElement(metadata, 'description').text = 'For the listed Kodi prerelease APIs only.'
        ET.SubElement(metadata, 'platform').text = 'all'
        ET.SubElement(ET.SubElement(metadata, 'assets'), 'icon').text = 'icon.png'
    else:
        addon = preview
        repo = addon.find(REPO_POINT)
        if version_tuple(release.api) < version_tuple(latest_directory(addon).attrib['maxversion']):
            return None

    entry = repo.find(f'dir[@minversion="{release.api}"]')
    if entry is not None:
        checksum = entry.find('checksum')
        marker = next((child for child in entry if child.tag is ET.Comment
                       and (child.text or '').startswith(PREVIEW_RELEASE_MARKER)), None)
        if checksum.get('verify') and not result.verify:
            return None
        if (entry.findtext('info'), checksum.text, checksum.get('verify'), entry.findtext('datadir')) == (
                result.info, result.checksum, result.verify, result.datadir) and (
                marker is None or marker.text == PREVIEW_RELEASE_MARKER + release.tag):
            return None
        if marker is not None:
            entry.remove(marker)
    else:
        entry = ET.SubElement(repo, 'dir', {'minversion': release.api, 'maxversion': release.api})
        for field in ('info', 'checksum', 'datadir', 'hashes'):
            ET.SubElement(entry, field)
        entry.find('hashes').text = 'false'
    entry.insert(0, ET.Comment(PREVIEW_RELEASE_MARKER + release.tag))
    entry.find('info').text = result.info
    checksum = entry.find('checksum')
    checksum.text = result.checksum
    if result.verify:
        checksum.set('verify', result.verify)
    entry.find('datadir').text = result.datadir
    if preview is not None:
        major, minor, patch = version_tuple(addon.attrib['version'])
        addon.set('version', f'{major}.{minor}.{patch + 1}')
    ET.indent(addon, space='  ')
    return ET.tostring(addon, encoding='utf-8', xml_declaration=True) + b'\n'


def sync(root, release, *, dry_run=False, channel='stable'):
    addons = {path: parse_manifest(path, data) for path, data in load_manifests(root, 'stable').items()}
    previews = ({path: parse_manifest(path, data) for path, data in load_manifests(root, 'preview').items()}
                if channel == 'preview' else {})

    def target_path(path):
        if channel == 'preview':
            return root / (path.parent.name + PREVIEW_SUFFIX) / 'addon.xml'
        return path

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {}
        for path, addon in addons.items():
            datadir, info = mirror_urls(addon, release)
            previous = latest_directory(previews.get(target_path(path), addon))
            current = previous.find('checksum')
            existing_checksum = (current.text if current.get('verify') == 'sha256'
                                 and previous.findtext('info') == info else None)
            jobs[path] = pool.submit(probe_mirror, datadir, info, existing_checksum)
        results = {path: job.result() for path, job in jobs.items()}

    updates = {}
    report = [f'### Kodi {release.tag} · {channel} · Add-on API {release.api}', '']
    for path, result in results.items():
        target = target_path(path)
        if channel == 'preview':
            manifest = adapt_preview(addons[path], previews.get(target), release, result)
        else:
            manifest = adapt(addons[path], release, result)
        if manifest is not None:
            updates[target] = manifest
        if result.error:
            status = f'skipped ({result.error})'
        else:
            mode = 'verified SHA-256' if result.verify else 'legacy index change marker'
            status = f'{"candidate update" if manifest is not None else "already configured"} ({mode})'
        report.append(f'- `{target.parent.name}` — {status}')

    summary = '\n'.join(report) + '\n'
    print(summary, end='')
    if summary_path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(summary_path).open('a', encoding='utf-8') as stream:
            stream.write(summary)
    if all(result.error for result in results.values()):
        raise ValueError(f'{release.tag}: no mirror served a usable index')
    if updates and not dry_run:
        build(root, updates=updates)
    if output := os.environ.get('GITHUB_OUTPUT'):
        with Path(output).open('a', encoding='utf-8') as stream:
            stream.write(f'updated={str(bool(updates) and not dry_run).lower()}\n')
    return updates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', choices=('stable', 'preview'), default='stable')
    parser.add_argument('--dry-run', action='store_true', help='Probe mirrors without writing repository files')
    args = parser.parse_args()
    try:
        release = fetch_latest_release(os.environ.get('GH_TOKEN'), channel=args.channel)
        if release is None:
            print('No active Kodi prerelease newer than the current stable release.')
            return 0
        sync(ROOT, release, dry_run=args.dry_run, channel=args.channel)
    except (OSError, ValueError, ET.ParseError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
