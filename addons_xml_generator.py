#!/usr/bin/env python3
"""Build and validate the Kodi repository index and versioned ZIPs."""

import argparse
import hashlib
import io
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parent
MIRROR_PREFIX = 'repository.kodi.mirror.'
PREVIEW_SUFFIX = '.preview'


def version_tuple(value):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value):
        raise ValueError(f'Invalid version: {value!r}')
    return tuple(map(int, value.split('.')))


def parse_manifest(path, data):
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        addon = ET.fromstring(data, parser=parser)
    except ET.ParseError as error:
        raise ValueError(f'{path}: {error}') from error
    if addon.tag != 'addon' or addon.get('id') != path.parent.name:
        raise ValueError(f'{path}: add-on ID must match its directory')
    version_tuple(addon.get('version', ''))
    repo = addon.find("extension[@point='xbmc.addon.repository']")
    dirs = repo.findall('dir') if repo is not None else []
    if not dirs:
        raise ValueError(f'{path}: missing repository directories')
    ranges = []
    for entry in dirs:
        low = version_tuple(entry.get('minversion', ''))
        high = version_tuple(entry.get('maxversion', ''))
        if low > high:
            raise ValueError(f'{path}: reversed version range {low} to {high}')
        ranges.append((low, high))
        for field in ('info', 'checksum', 'datadir'):
            if not entry.findtext(field, '').startswith('https://'):
                raise ValueError(f'{path}: missing HTTPS {field} URL')
    ranges.sort()
    if any(end >= start for (_, end), (start, _) in zip(ranges, ranges[1:])):
        raise ValueError(f'{path}: overlapping Kodi API version ranges')
    return addon


def load_manifests(root, channel=None):
    manifests = {}
    for directory in sorted(root.glob(f'{MIRROR_PREFIX}*')):
        is_preview = directory.name.endswith(PREVIEW_SUFFIX)
        if directory.is_dir() and (channel is None or is_preview == (channel == 'preview')):
            path = directory / 'addon.xml'
            manifests[path] = path.read_bytes()
    if not manifests and channel != 'preview':
        raise ValueError(f'{root}: no mirror manifests found')
    return manifests


def make_zip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data in files.items():
            member = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            # Store the two small entries to produce identical bytes across zlib versions.
            archive.writestr(member, data)
    return output.getvalue()


def check_zip(path, files):
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(entry.filename for entry in archive.infolist() if not entry.is_dir())
            # Allow the optional root directory entry in older packages.
            directories = [entry.filename for entry in archive.infolist() if entry.is_dir()]
            root_entry = next(iter(files)).split('/')[0] + '/'
            if names == sorted(files) and directories in ([], [root_entry]):
                for name, source in files.items():
                    packed = archive.read(name)
                    if name.endswith('/addon.xml'):
                        # Historical ZIPs may retain CRLF after a checkout normalizes the file.
                        packed = packed.replace(b'\r\n', b'\n')
                        source = source.replace(b'\r\n', b'\n')
                    if packed != source:
                        break
                else:
                    return
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError(f'{path}: invalid ZIP ({error})') from error
    raise ValueError(f'{path}: package differs from its sources, bump the add-on version')


def write_atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        pending = Path(temp.name)
    try:
        pending.write_bytes(data)
        pending.chmod(0o644)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def build(root, *, check=False, updates=None):
    """Validate inputs before writing files and preserve existing versioned packages."""
    manifests = load_manifests(root)
    updates = updates or {}
    entries = {'stable': [], 'preview': []}
    packages = {}
    icons = {}
    for path, data in sorted((manifests | updates).items()):
        preview = path.parent.name.endswith(PREVIEW_SUFFIX)
        stable_id = path.parent.name.removesuffix(PREVIEW_SUFFIX)
        stable_dir = root / stable_id
        if path not in manifests and (not preview or path != root / (stable_id + PREVIEW_SUFFIX) / 'addon.xml'
                                      or stable_dir / 'addon.xml' not in manifests):
            raise ValueError(f'{path}: no corresponding stable mirror')
        addon = parse_manifest(path, data)
        text = data.decode('utf-8-sig')
        if text.startswith('<?xml'):
            text = text.partition('?>')[2]
        entries['preview' if preview else 'stable'].append(
            '\n'.join(line.rstrip() for line in text.splitlines()).strip())
        addon_id = addon.attrib['id']
        version = addon.attrib['version']
        icon_path = path.with_name('icon.png')
        if preview and path not in manifests and not icon_path.exists():
            icons[icon_path] = (stable_dir / 'icon.png').read_bytes()
        icon = icons[icon_path] if icon_path in icons else icon_path.read_bytes()
        files = {f'{addon_id}/addon.xml': data, f'{addon_id}/icon.png': icon}
        archive_path = path.parent / f'{addon_id}-{version}.zip'
        if archive_path.exists():
            check_zip(archive_path, files)
        else:
            packages[archive_path] = make_zip(files)

    expected = packages | icons | updates
    for channel, name in (('stable', 'addons.xml'), ('preview', 'addons.preview.xml')):
        path = root / name
        if channel == 'preview' and not entries[channel] and not path.exists():
            continue
        header = '<?xml version="1.0" encoding="UTF-8"?>\n<addons>\n'
        xml = (header + '\n\n'.join(entries[channel]) + '\n</addons>\n').encode('utf-8')
        try:
            ET.fromstring(xml)
        except ET.ParseError as error:
            raise ValueError(f'Generated invalid {name}: {error}') from error
        expected[path] = xml
        expected[root / (name + '.md5')] = hashlib.md5(xml).hexdigest().encode('ascii')
    changed = [path for path, data in expected.items()
               if not path.exists() or path.read_bytes() != data]
    if check and changed:
        raise ValueError('Regenerate missing or stale files: ' + ', '.join(
            str(path.relative_to(root)) for path in changed))
    for path in changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, expected[path])
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate without writing files')
    args = parser.parse_args()
    try:
        changed = build(ROOT, check=args.check)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f'Updated {len(changed)} repository files.' if changed else 'Repository files are current.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
