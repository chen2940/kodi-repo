#!/usr/bin/env python3
"""Publish a GitHub Release only when a channel has a new ZIP snapshot."""

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from addons_xml_generator import ROOT, build, load_manifests, parse_manifest, version_tuple
from sync_kodi_release import PREVIEW_RELEASE_MARKER, RELEASE_TAG, REPO_POINT


BOOTSTRAP_PREVIEW_TAGS = {('piers', '21.90.902'): '22.0rc2-Piers'}


@dataclass(frozen=True)
class KodiTarget:
    api: str
    label: str


def target(entry, channel):
    api = entry.attrib['maxversion']
    series = entry.findtext('datadir').rstrip('/').rsplit('/', 1)[-1]
    if channel == 'preview':
        tag = next((child.text.removeprefix(PREVIEW_RELEASE_MARKER) for child in entry
                    if child.tag is ET.Comment and (child.text or '').startswith(PREVIEW_RELEASE_MARKER)),
                   BOOTSTRAP_PREVIEW_TAGS.get((series, api), ''))
        match = RELEASE_TAG.fullmatch(tag)
        if match is not None and match['stage']:
            stage = match['stage'].rstrip('0123456789')
            number = match['stage'][len(stage):]
            name = {'a': 'Alpha', 'b': 'Beta', 'rc': 'RC'}[stage]
            return KodiTarget(api, f"Kodi {match['number']} {name}{number} {series.capitalize()}")
        return KodiTarget(api, f'Kodi {series.capitalize()} preview (Add-on API {api})')
    display_version = api.removesuffix('.0')
    return KodiTarget(api, f'Kodi {display_version} {series.capitalize()}')


@dataclass(frozen=True)
class Snapshot:
    channel: str
    tag: str
    assets: tuple[Path, ...]
    targets: tuple[tuple[KodiTarget, ...], ...]


def snapshot(root, channel):
    assets = []
    targets = []
    digest = hashlib.sha256()
    for path, data in load_manifests(root, channel).items():
        addon = parse_manifest(path, data)
        filename = f'{addon.attrib["id"]}-{addon.attrib["version"]}.zip'
        archive = path.with_name(filename)
        digest.update(filename.encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(archive.read_bytes()).digest())
        assets.append(archive)
        dirs = addon.findall(f'{REPO_POINT}/dir')
        versions = tuple(target(entry, channel) for entry in dirs)
        if channel == 'stable':
            versions = (max(versions, key=lambda value: version_tuple(value.api)),)
        targets.append(versions)
    if not assets:
        return None
    return Snapshot(channel, f'mirrors-{channel}-{digest.hexdigest()[:20]}', tuple(assets), tuple(targets))


def github(*args, capture=False):
    result = subprocess.run(['gh', *args], check=True, text=True, encoding='utf-8',
                            capture_output=capture)
    return result.stdout


def release_metadata(commit, item):
    preview = item.channel == 'preview'
    newest = max((value for versions in item.targets for value in versions),
                 key=lambda value: version_tuple(value.api))
    title = f'Kodi mirror repositories — {newest.label} ({item.channel})'
    notes = [f'Repository snapshot: `{commit}`.', '',
             'Install the ZIP for your preferred mirror via Kodi’s Install from zip file.', '',
             '### Kodi compatibility',
             ('Listed prerelease builds only.' if preview else
              'Highest compatible Kodi version per ZIP. Older ranges, where supported, are in the ZIP manifest.'), '']
    for path, versions in zip(item.assets, item.targets):
        labels = ', '.join(f'{value.label} (Add-on API `{value.api}`)'
                           if 'Add-on API' not in value.label else value.label for value in versions)
        notes.append(f'- `{path.name}` — {labels}')
    return title, '\n'.join(notes)


def publish(repo, commit, item, releases, *, refresh_metadata=False):
    existing = releases.get(item.tag)
    if existing is not None and not existing['draft']:
        if refresh_metadata:
            title, notes = release_metadata(existing.get('target_commitish') or commit, item)
            github('release', 'edit', item.tag, '--repo', repo, '--title', title, '--notes', notes)
        print(f'{item.channel}: {item.tag} already published')
        return

    preview = item.channel == 'preview'
    latest = '--latest=false' if preview else '--latest=true'
    assets = [str(path) for path in item.assets]
    title, notes = release_metadata(commit, item)
    if existing is not None:
        github('release', 'upload', item.tag, '--repo', repo, '--clobber', *assets)
        github('release', 'edit', item.tag, '--repo', repo, '--draft=false', latest,
               '--title', title, '--notes', notes)
        return

    flags = ['--prerelease'] if preview else []
    github('release', 'create', item.tag, '--repo', repo, '--target', commit,
           '--title', title, '--notes', notes, latest, *flags, *assets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--repo', required=True, help='GitHub owner/repository')
    parser.add_argument('--commit', required=True, help='Full SHA of the validated checkout')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--refresh-metadata', action='store_true', help='Also update existing Release titles and notes')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('--repo must be a GitHub owner/repository')
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.error('--commit must be a full commit SHA')

    try:
        root = args.root.resolve()
        build(root, check=True)
        snapshots = [item for channel in ('stable', 'preview') if (item := snapshot(root, channel))]
        for item in snapshots:
            print(f'{item.tag}: {len(item.assets)} ZIPs', flush=True)
        if args.dry_run:
            for item in snapshots:
                title, notes = release_metadata(args.commit, item)
                print(title, notes, sep='\n')
            return 0
        pages = json.loads(github('api', '--paginate', '--slurp',
                                  f'repos/{args.repo}/releases?per_page=100', capture=True))
        releases = {release['tag_name']: release for page in pages for release in page}
        for item in snapshots:
            publish(args.repo, args.commit, item, releases, refresh_metadata=args.refresh_metadata)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.rstrip(), file=sys.stderr)
        print(f'Release publication failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
