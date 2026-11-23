import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import addons_xml_generator as builder
import sync_kodi_release as sync


MIRROR = 'https://mirror.example/xbmc/addons'
ADDON_ID = builder.MIRROR_PREFIX + 'example'
MANIFEST = f'''<?xml version="1.0" encoding="UTF-8"?>
<addon id="{ADDON_ID}" version="0.1.4">
  <extension point="xbmc.addon.repository">
    <dir minversion="21.0.0" maxversion="21.3.0">
      <info>{MIRROR}/omega/addons.xml.gz</info>
      <checksum>{MIRROR}/omega/addons.xml.gz</checksum>
      <datadir>{MIRROR}/omega</datadir>
      <hashes>false</hashes>
    </dir>
  </extension>
  <extension point="xbmc.addon.metadata">
    <summary lang="zh">示例镜像</summary>
  </extension>
</addon>
'''.encode('utf-8-sig')


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manifest = self.root / ADDON_ID / 'addon.xml'
        self.manifest.parent.mkdir()
        self.manifest.write_bytes(MANIFEST)
        self.manifest.with_name('icon.png').write_bytes(b'example-icon')

    def package(self, version):
        return self.manifest.parent / f'{ADDON_ID}-{version}.zip'

    def test_build_repairs_index_and_emits_reproducible_package(self):
        index = self.root / 'addons.xml'
        index.write_bytes(b'<addons />\n</html>')
        with self.assertRaisesRegex(ValueError, 'stale files'):
            builder.build(self.root, check=True)
        self.assertTrue(index.read_bytes().endswith(b'</html>'))
        self.assertEqual(len(builder.build(self.root)), 3)
        self.assertEqual(index.read_bytes().decode('utf-8').count('示例镜像'), 1)
        self.assertEqual(ET.parse(index).getroot().tag, 'addons')
        self.assertEqual((self.root / 'addons.xml.md5').read_text(), hashlib.md5(index.read_bytes()).hexdigest())
        archive = self.package('0.1.4')
        expected = archive.read_bytes()
        archive.unlink()
        builder.build(self.root)
        self.assertEqual(archive.read_bytes(), expected)
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(package.read(f'{ADDON_ID}/addon.xml'), MANIFEST)
            self.assertIsNone(package.testzip())
        self.assertEqual(builder.build(self.root, check=True), [])

    def test_missing_or_bad_manifest_fails_before_writing(self):
        self.manifest.write_bytes(b'<addon>')
        with self.assertRaisesRegex(ValueError, 'addon.xml'):
            builder.build(self.root)
        self.assertFalse(self.package('0.1.4').exists())
        self.assertFalse((self.root / 'addons.xml').exists())

    def test_historical_package_matches_after_checkout_normalizes_line_endings(self):
        self.manifest.write_bytes(MANIFEST.replace(b'\n', b'\r\n'))
        builder.build(self.root)
        self.manifest.write_bytes(MANIFEST)
        self.assertEqual(builder.build(self.root, check=True), [])

    def test_published_version_cannot_be_overwritten(self):
        builder.build(self.root)
        archive = self.package('0.1.4')
        original = archive.read_bytes()
        self.manifest.with_name('icon.png').write_bytes(b'new-icon')
        with self.assertRaisesRegex(ValueError, 'bump the add-on version'):
            builder.build(self.root)
        self.assertEqual(archive.read_bytes(), original)

    def test_new_series_sync_skips_failed_mirror_and_is_idempotent(self):
        builder.build(self.root)
        old_package = self.package('0.1.4').read_bytes()
        failed = ADDON_ID + '-offline'
        failed_manifest = self.root / failed / 'addon.xml'
        failed_manifest.parent.mkdir()
        failed_manifest.write_bytes(MANIFEST.replace(ADDON_ID.encode(), failed.encode())
                                          .replace(b'https://mirror.example', b'https://offline.example'))
        failed_manifest.with_name('icon.png').write_bytes(b'example-icon')
        release = sync.Release('22.0-Piers', '22.0.0', 'piers', 'addons.xml.gz')

        def probe(datadir, info, existing_checksum):
            if 'offline.example' in info:
                return sync.MirrorResult(datadir, info, error='HTTP 404')
            return sync.MirrorResult(datadir, info, info + '.sha256', 'sha256')

        with patch.object(sync, 'probe_mirror', side_effect=probe), contextlib.redirect_stdout(io.StringIO()):
            self.assertIn(self.manifest, sync.sync(self.root, release, dry_run=True))
            self.assertEqual(self.manifest.read_bytes(), MANIFEST)
            self.assertEqual(list(sync.sync(self.root, release)), [self.manifest])
            self.assertEqual(sync.sync(self.root, release), {})

        addon = ET.parse(self.manifest).getroot()
        dirs = addon.findall(f'{sync.REPO_POINT}/dir')
        self.assertEqual(len(dirs), 2)
        self.assertEqual(dirs[1].attrib, {'minversion': '22.0.0', 'maxversion': '22.0.0'})
        self.assertEqual(dirs[1].find('checksum').get('verify'), 'sha256')
        self.assertEqual(addon.get('version'), '0.1.5')
        self.assertEqual(ET.parse(failed_manifest).getroot().get('version'), '0.1.4')
        self.assertEqual(self.package('0.1.4').read_bytes(), old_package)
        self.assertTrue(self.package('0.1.5').exists())
        self.assertEqual(builder.build(self.root, check=True), [])

    def test_same_series_extends_existing_range_without_adding_directory(self):
        addon = builder.parse_manifest(self.manifest, MANIFEST)
        release = sync.Release('21.4-Omega', '21.4.0', 'omega', 'addons.xml.gz')
        datadir, info = sync.mirror_urls(addon, release)
        updated = sync.adapt(addon, release, sync.MirrorResult(datadir, info, info))
        dirs = ET.fromstring(updated).findall(f'{sync.REPO_POINT}/dir')
        self.assertEqual(len(dirs), 1)
        self.assertEqual(dirs[0].attrib, {'minversion': '21.0.0', 'maxversion': '21.4.0'})

    def test_preview_creates_separate_packages_and_exact_api_entries(self):
        builder.build(self.root)
        stable_index = (self.root / 'addons.xml').read_bytes()
        stable_zip = self.package('0.1.4').read_bytes()
        first = sync.Release('22.0rc1-Piers', '21.90.901', 'piers', 'addons.xml.gz')
        second = sync.Release('22.0rc2-Piers', '21.90.902', 'piers', 'addons.xml.gz')

        def probe(datadir, info, existing_checksum):
            return sync.MirrorResult(datadir, info, info)

        preview_id = ADDON_ID + '.preview'
        manifest = self.root / preview_id / 'addon.xml'
        preview_zip = manifest.parent / f'{preview_id}-1.0.0.zip'
        with patch.object(sync, 'probe_mirror', side_effect=probe), contextlib.redirect_stdout(io.StringIO()):
            self.assertIn(manifest, sync.sync(self.root, first, dry_run=True, channel='preview'))
            self.assertFalse(manifest.parent.exists())
            self.assertIn(manifest, sync.sync(self.root, first, channel='preview'))
            initial_zip = preview_zip.read_bytes()
            self.assertIn(manifest, sync.sync(self.root, second, channel='preview'))
            self.assertEqual(sync.sync(self.root, second, channel='preview'), {})
            stable_patch = sync.Release('21.4-Omega', '21.4.0', 'omega', 'addons.xml.gz')
            self.assertEqual(list(sync.sync(self.root, stable_patch, dry_run=True)), [self.manifest])

        self.assertEqual((self.root / 'addons.xml').read_bytes(), stable_index)
        self.assertEqual(self.package('0.1.4').read_bytes(), stable_zip)
        self.assertEqual(preview_zip.read_bytes(), initial_zip)
        preview_index = self.root / 'addons.preview.xml'
        addon, = ET.parse(preview_index).getroot()
        self.assertEqual(addon.get('id'), preview_id)
        self.assertEqual(addon.get('version'), '1.0.1')
        self.assertEqual([entry.attrib for entry in addon.findall(f'{sync.REPO_POINT}/dir')], [
            {'minversion': '21.90.901', 'maxversion': '21.90.901'},
            {'minversion': '21.90.902', 'maxversion': '21.90.902'},
        ])
        self.assertIn(b'<!--kodi-release: 22.0rc2-Piers-->', manifest.read_bytes())
        self.assertEqual((self.root / 'addons.preview.xml.md5').read_text(),
                         hashlib.md5(preview_index.read_bytes()).hexdigest())
        self.assertEqual(builder.build(self.root, check=True), [])


class UpstreamTests(unittest.TestCase):
    def test_latest_release_reads_api_and_repository_from_tag(self):
        meta = {'tag_name': '22.0-Piers', 'prerelease': False, 'draft': False}
        repo = b'''<addon><extension point="xbmc.addon.repository"><dir>
          <info>https://mirrors.kodi.tv/addons/piers/addons.xml.gz</info>
          <datadir>https://mirrors.kodi.tv/addons/piers</datadir>
          </dir></extension></addon>'''
        with patch.object(sync, 'download', side_effect=[json.dumps(meta).encode(), b'ADDON_API 22.0.1\n', repo]) as download:
            self.assertEqual(sync.fetch_latest_release('test-token'),
                             sync.Release('22.0-Piers', '22.0.1', 'piers', 'addons.xml.gz'))
        self.assertIn('22.0-Piers/version.txt', download.call_args_list[1].args[0])
        self.assertNotIn('headers', download.call_args_list[1].kwargs)

    def test_rejects_unexpected_prerelease(self):
        meta = {'tag_name': '22.0rc2-Piers', 'prerelease': True, 'draft': False}
        with patch.object(sync, 'download', return_value=json.dumps(meta).encode()):
            with self.assertRaisesRegex(ValueError, 'Unexpected stable Kodi release'):
                sync.fetch_latest_release()

    def test_preview_ignores_claimed_newest_date_and_stale_or_draft_releases(self):
        stable = {'tag_name': '21.3-Omega', 'prerelease': False, 'draft': False}
        entries = [{'tag_name': tag, 'prerelease': True, 'draft': False} for tag in (
            '22.0b3-Piers', '21.3rc7-Omega', '22.0rc2-Piers', '22.0rc1-Piers', '22.0a10-Piers')]
        entries.append({'tag_name': '23.0a1-Next', 'prerelease': True, 'draft': True})
        repo = b'''<addon><extension point="xbmc.addon.repository"><dir>
          <info>https://mirrors.kodi.tv/addons/piers/addons.xml.gz</info>
          <datadir>https://mirrors.kodi.tv/addons/piers</datadir>
          </dir></extension></addon>'''
        data = [json.dumps(stable).encode(), json.dumps(entries).encode(), b'ADDON_API 21.90.902\n', repo]
        with patch.object(sync, 'download', side_effect=data) as download:
            self.assertEqual(sync.fetch_latest_release(channel='preview'),
                             sync.Release('22.0rc2-Piers', '21.90.902', 'piers', 'addons.xml.gz'))
        self.assertIn('22.0rc2-Piers', download.call_args_list[2].args[0])

    def test_preview_noops_after_prerelease_graduates_to_stable(self):
        stable = {'tag_name': '22.0-Piers', 'prerelease': False, 'draft': False}
        old = [{'tag_name': '22.0rc2-Piers', 'prerelease': True, 'draft': False}]
        with patch.object(sync, 'download', side_effect=[json.dumps(stable).encode(), json.dumps(old).encode()]):
            self.assertIsNone(sync.fetch_latest_release(channel='preview'))


class MirrorTests(unittest.TestCase):
    datadir = f'{MIRROR}/piers'
    info = f'{datadir}/addons.xml.gz'
    data = gzip.compress(b'<addons><addon id="plugin.example" version="1.0.0"/></addons>')

    def test_uses_only_matching_sha256(self):
        digest = hashlib.sha256(self.data).hexdigest().encode()
        with patch.object(sync, 'download', side_effect=[self.data, digest]):
            result = sync.probe_mirror(self.datadir, self.info)
        self.assertEqual(result.checksum, self.info + '?sha256')
        self.assertEqual(result.verify, 'sha256')
        self.assertIsNone(result.error)

    def test_allows_legacy_mirror_but_never_downgrades_verified_checksum(self):
        with patch.object(sync, 'download', side_effect=[self.data, self.data, OSError('404')]):
            result = sync.probe_mirror(self.datadir, self.info)
            self.assertEqual(result.checksum, self.info)
            self.assertIsNone(result.verify)
        with patch.object(sync, 'download', side_effect=[self.data, OSError('404'), self.data]):
            result = sync.probe_mirror(self.datadir, self.info, self.info + '.sha256')
            self.assertIn('could not be verified', result.error)

    def test_bad_digest_and_broken_index_are_rejected(self):
        with patch.object(sync, 'download', side_effect=[self.data, b'0' * 64, OSError('404')]):
            self.assertIn('does not match', sync.probe_mirror(self.datadir, self.info).error)
        with patch.object(sync, 'download', return_value=b'<html>not an index</html>'):
            self.assertIsNotNone(sync.probe_mirror(self.datadir, self.info).error)


if __name__ == '__main__':
    unittest.main()
