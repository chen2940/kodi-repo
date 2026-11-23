from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publish_kodi_releases as release

from test_repository import ADDON_ID, MANIFEST


class SnapshotTests(unittest.TestCase):
    def test_change_detection_uses_only_current_channel_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mirror = root / ADDON_ID
            mirror.mkdir()
            manifest = mirror / 'addon.xml'
            manifest.write_bytes(MANIFEST)
            (mirror / 'icon.png').write_bytes(b'example-icon')
            release.build(root)
            stable = release.snapshot(root, 'stable')
            (root / 'README.md').write_text('documentation change', encoding='utf-8')
            self.assertEqual(release.snapshot(root, 'stable'), stable)
            self.assertIsNone(release.snapshot(root, 'preview'))
            release.build(root, updates={manifest: MANIFEST.replace(b'0.1.4', b'0.1.5')})
            followup = release.snapshot(root, 'stable')
            self.assertNotEqual(followup.tag, stable.tag)
            self.assertEqual(followup.assets, (mirror / f'{ADDON_ID}-0.1.5.zip',))


class PublishTests(unittest.TestCase):
    repo = 'example/kodi-repo'
    commit = 'a' * 40

    def item(self, channel):
        target = release.KodiTarget('21.90.902', 'Kodi 22.0 RC2 Piers') if channel == 'preview' else (
            release.KodiTarget('21.3.0', 'Kodi 21.3 Omega'))
        return release.Snapshot(channel, f'mirrors-{channel}-1234', (Path('mirror.zip'),), ((target,),))

    def test_unchanged_published_release_is_not_edited(self):
        item = self.item('stable')
        with patch.object(release, 'github') as github:
            release.publish(self.repo, self.commit, item, {item.tag: {'draft': False}})
        github.assert_not_called()

    def test_stable_and_preview_have_different_release_flags(self):
        for channel in ('stable', 'preview'):
            with self.subTest(channel=channel), patch.object(release, 'github') as github:
                release.publish(self.repo, self.commit, self.item(channel), {})
                args = github.call_args.args
                preview = channel == 'preview'
                self.assertEqual(args[:2], ('release', 'create'))
                self.assertIn('--latest=false' if preview else '--latest=true', args)
                self.assertEqual('--prerelease' in args, preview)
                self.assertIn('Kodi 22.0 RC2 Piers' if preview else 'Kodi 21.3 Omega',
                              args[args.index('--title') + 1])
                self.assertIn('21.90.902' if preview else '21.3.0', args[args.index('--notes') + 1])
                self.assertEqual(args[-1], 'mirror.zip')

    def test_metadata_refresh_does_not_change_assets_or_release_flags(self):
        item = self.item('stable')
        with patch.object(release, 'github') as github:
            release.publish(self.repo, self.commit, item, {item.tag: {'draft': False}}, refresh_metadata=True)
        github.assert_called_once()
        args = github.call_args.args
        self.assertEqual(args[:2], ('release', 'edit'))
        self.assertIn('Kodi 21.3 Omega', args[args.index('--title') + 1])
        self.assertFalse(any(arg.startswith(('--latest', '--prerelease', '--draft')) for arg in args))

    def test_draft_upload_must_succeed_before_publishing(self):
        item = self.item('preview')
        with patch.object(release, 'github', side_effect=OSError('upload failed')) as github:
            with self.assertRaisesRegex(OSError, 'upload failed'):
                release.publish(self.repo, self.commit, item, {item.tag: {'draft': True}})
        self.assertEqual(github.call_count, 1)
        self.assertEqual(github.call_args.args[:2], ('release', 'upload'))
        with patch.object(release, 'github') as github:
            release.publish(self.repo, self.commit, item, {item.tag: {'draft': True}})
        self.assertEqual(github.call_args.args[:2], ('release', 'edit'))
        self.assertIn('--draft=false', github.call_args.args)
