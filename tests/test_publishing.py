import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from tools import publishing as p
import yt_autoupload as u
import yt_autowatch as w
import metadata as m

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1sAAAAASUVORK5CYII=')
META = 'title:\nTown Walk\n\ntitle_alternatives:\nOther Title\n\ndescription:\nA walk through town.\n\nprivacy: unlisted\n\ntags: #Travel, #Film\n'

class PublishingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        for state in ('done', 'prepared', 'failed', 'logs', 'secrets'):
            (self.base / state).mkdir()
        self.folder = self.base / 'done/EPISODE'
        self.folder.mkdir()
        (self.folder / 'meta.txt').write_text(META)
        (self.folder / 'youtube_video_id.txt').write_text('abcdefghijk')
        (self.folder / 'thumbnail.png').write_bytes(PNG)
        for path in self.folder.iterdir(): os.utime(path, (1, 1))
        self.patch(p, 'BASE', self.base)
        self.patch(u, 'BASE', self.base)
        self.patch(u, 'DONE', self.base / 'done')
        self.patch(u, 'PREPARED', self.base / 'prepared')
        self.patch(u, 'FAILED', self.base / 'failed')
        self.patch(w, 'BASE', self.base)
        self.patch(m, 'BASE', self.base)
        self.patch(u, 'log', MagicMock())
        # Fail closed if any test accidentally reaches real auth/network/upload.
        self.auth = self.patch(p, 'update_service', MagicMock(side_effect=AssertionError('LIVE AUTH FORBIDDEN')))
        self.patch(u, 'get_upload_service', MagicMock(side_effect=AssertionError('LIVE UPLOAD FORBIDDEN')))
        self.patch(u, 'get_check_service', MagicMock(side_effect=AssertionError('LIVE CHECK FORBIDDEN')))
        self.service = MagicMock()
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': [
            {'id': 'abcdefghijk', 'snippet': {'title': 'Old', 'description': 'Old', 'categoryId': '19',
             'defaultLanguage': 'en', 'defaultAudioLanguage': 'en', 'tags': ['old'], 'channelId': 'readonly'}}]}
        self.service.videos.return_value.update.return_value.execute.return_value = {'id': 'abcdefghijk'}
        self.service.thumbnails.return_value.set.return_value.execute.return_value = {'items': [{'default': {'url': 'image'}}]}

    def patch(self, obj, name, value):
        item = patch.object(obj, name, value); item.start(); self.addCleanup(item.stop)
        return value

    def ready(self):
        self.assertIn('WAIT package stability', p.process(self.folder, run=True, now=1000))
        return p.process(self.folder, run=True, now=1121)

    def apply(self, now=1122):
        return p.process(self.folder, run=True, apply=True, service=self.service, now=now)

    def test_real_image_decoder_and_existing_thumbnail_precedence(self):
        (self.folder / 'Thumbnail - 27.PNG').write_bytes(PNG)
        self.assertEqual(p.snapshot(self.folder)['thumbnail'], 'thumbnail.png')
        (self.folder / 'thumbnail.png').unlink()
        self.assertEqual(p.snapshot(self.folder)['thumbnail'], 'Thumbnail - 27.PNG')

    def test_dry_run_has_no_writes_or_auth_even_when_apply_true(self):
        before = {q.name: q.read_bytes() for q in self.folder.iterdir()}
        self.assertIn('PACKAGE VALID', p.process(self.folder, apply=True))
        self.assertEqual(before, {q.name: q.read_bytes() for q in self.folder.iterdir()})
        self.auth.assert_not_called()

    def test_readiness_and_idempotent_update(self):
        self.assertEqual(self.ready(), 'ALL_READY / UPDATE DISABLED')
        marker = json.loads((self.folder / 'ALL_READY').read_text())
        self.assertEqual(marker['video_id'], 'abcdefghijk')
        self.assertEqual(self.apply(), 'ALL_READY / APPLIED')
        self.assertIn('unchanged; skipped', self.apply())
        self.service.videos.return_value.update.assert_called_once()
        self.service.thumbnails.return_value.set.assert_called_once()
        self.service.videos.return_value.insert.assert_not_called()
        self.assertEqual((self.folder / 'meta.txt').read_text(), META)
        self.assertEqual((self.folder / 'thumbnail.png').read_bytes(), PNG)

    def test_preserve_privacy_remote_category_language_and_alternative_titles(self):
        self.ready(); self.apply()
        args = self.service.videos.return_value.update.call_args.kwargs
        self.assertEqual(args['part'], 'snippet')
        self.assertNotIn('status', args['body'])
        snippet = args['body']['snippet']
        self.assertEqual(snippet['title'], 'Town Walk')
        self.assertEqual(snippet['categoryId'], '19')
        self.assertEqual(snippet['defaultLanguage'], 'en')
        self.assertEqual(snippet['defaultAudioLanguage'], 'en')
        self.assertNotIn('title_alternatives', snippet)
        self.assertNotIn('channelId', snippet)

    def test_thumbnail_failure_keeps_metadata_checkpoint_and_retries(self):
        self.ready()
        self.service.thumbnails.return_value.set.return_value.execute.side_effect = OSError('offline')
        self.assertIn('pending', self.apply())
        self.assertEqual(self.apply(now=1123), 'ALL_READY / UPDATE RETRY PENDING')
        self.service.thumbnails.return_value.set.return_value.execute.side_effect = None
        self.assertEqual(self.apply(now=5000), 'ALL_READY / APPLIED')
        self.service.videos.return_value.update.assert_called_once()
        self.assertEqual(self.service.thumbnails.return_value.set.call_count, 2)

    def test_metadata_failure_does_not_apply_thumbnail(self):
        self.ready()
        self.service.videos.return_value.update.return_value.execute.side_effect = OSError('offline')
        self.assertIn('pending', self.apply())
        self.service.thumbnails.return_value.set.assert_not_called()
        self.assertFalse(p.read_json(self.folder / '.publishing_state.json')['metadata_applied'])

    def test_missing_remote_video_never_updates_or_inserts(self):
        self.ready()
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': []}
        self.assertIn('pending', self.apply())
        self.service.videos.return_value.update.assert_not_called()
        self.service.videos.return_value.insert.assert_not_called()
        self.service.thumbnails.return_value.set.assert_not_called()

    def test_invalid_or_missing_inputs_remove_readiness(self):
        for name in ('meta.txt', 'thumbnail.png', 'youtube_video_id.txt'):
            with self.subTest(name=name):
                self.ready()
                path = self.folder / name; raw = path.read_bytes(); path.unlink()
                self.assertIn('WAIT', p.process(self.folder, run=True, now=1200))
                self.assertFalse((self.folder / 'ALL_READY').exists())
                path.write_bytes(raw); os.utime(path, (1, 1))
                (self.folder / '.publishing_candidate.json').unlink()

    def test_corrupt_image_not_ready(self):
        (self.folder / 'thumbnail.png').write_bytes(b'not a picture')
        self.assertIn('WAIT', p.process(self.folder, run=True))
        self.assertFalse((self.folder / 'ALL_READY').exists())

    def test_symlink_meta_refused(self):
        (self.base / 'manual.txt').write_text(META)
        (self.folder / 'meta.txt').unlink()
        (self.folder / 'meta.txt').symlink_to(self.base / 'manual.txt')
        self.assertIn('WAIT regular file', p.process(self.folder, run=True))

    def test_invalid_title_and_tags(self):
        for text in (META.replace('Town Walk', 'x' * 101), META.replace('#Travel, #Film', 'x' * 501), META.replace('A walk through town.', '')):
            (self.folder / 'meta.txt').write_text(text)
            self.assertIn('WAIT', p.process(self.folder))

    def test_edited_package_requires_stability_and_reapplies(self):
        self.ready(); self.apply()
        path = self.folder / 'meta.txt'; path.write_text(META.replace('Town Walk', 'New Walk')); os.utime(path, (1, 1))
        self.assertIn('WAIT package stability', self.apply(now=2000))
        self.assertFalse((self.folder / 'ALL_READY').exists())
        self.assertEqual(self.apply(now=2121), 'ALL_READY / APPLIED')
        self.assertEqual(self.service.videos.return_value.update.call_count, 2)

    def test_edit_during_metadata_update_blocks_thumbnail_and_clears_marker(self):
        self.ready()
        def change():
            (self.folder / 'meta.txt').write_text(META.replace('Town Walk', 'Changed'))
            return {'id': 'abcdefghijk'}
        self.service.videos.return_value.update.return_value.execute.side_effect = change
        self.assertIn('Package changed', self.apply())
        self.assertFalse((self.folder / 'ALL_READY').exists())
        self.service.thumbnails.return_value.set.assert_not_called()

    def test_file_age_prevents_premature_readiness(self):
        path = self.folder / 'thumbnail.png'; os.utime(path, (1050, 1050))
        p.process(self.folder, run=True, now=1000)
        self.assertIn('WAIT package stability', p.process(self.folder, run=True, now=1121))

    def test_prepared_project_cannot_be_all_ready(self):
        dest = self.base / 'prepared/EPISODE'; self.folder.rename(dest)
        self.assertEqual(p.process(dest, run=True, apply=True), 'WAIT verified project in done/')
        self.auth.assert_not_called()

    def test_direct_metadata_waits_for_done_even_with_final(self):
        dest = self.base / 'prepared/EPISODE'; self.folder.rename(dest)
        (dest / 'meta.txt').unlink(); (dest / 'subs').mkdir()
        (dest / 'subs/final.srt').write_text('not read before upload')
        with patch.object(m, 'request_metadata') as api:
            self.assertEqual(m.process(dest, run=True), 'WAIT verified upload in done/')
            api.assert_not_called()

    def test_done_missing_id_blocks_generation(self):
        (self.folder / 'meta.txt').unlink(); (self.folder / 'youtube_video_id.txt').unlink()
        (self.folder / 'subs').mkdir(); (self.folder / 'subs/final.srt').write_text('not read')
        self.assertEqual(m.process(self.folder, run=True), 'WAIT verified upload in done/')

    def test_existing_id_upload_one_never_uploads_video(self):
        with patch.object(u, 'finalize_verified_video', return_value='abcdefghijk') as finalize, patch.object(u, 'make_upload_request') as insert:
            self.assertEqual(u.upload_one(self.folder), 'abcdefghijk')
            finalize.assert_called_once_with(None, self.folder, 'abcdefghijk')
            insert.assert_not_called()

    def test_empty_existing_id_blocks_initial_upload(self):
        (self.folder / 'youtube_video_id.txt').write_text('')
        with self.assertRaises(u.VerificationPending): u.upload_one(self.folder)

    def test_watcher_dry_run_does_not_spawn_workers(self):
        with patch.object(sys, 'argv', ['yt_autowatch.py', '--dry-run']), patch.object(p, 'validate_image'), patch.object(w.subprocess, 'Popen') as spawn, contextlib.redirect_stdout(io.StringIO()) as output:
            w.main()
        spawn.assert_not_called()
        self.assertIn('PACKAGE VALID', output.getvalue())
        self.assertFalse((self.folder / 'ALL_READY').exists())

    def test_watcher_starts_completion_when_no_video_waiting(self):
        (self.base / 'tools').mkdir()
        (self.base / 'tools/publishing.py').touch()
        with patch.object(sys, 'argv', ['yt_autowatch.py']), patch.object(w, 'paths_lock', return_value=contextlib.nullcontext()), patch.object(w.subprocess, 'Popen') as spawn, patch.object(w.subprocess, 'run') as run:
            w.main()
        run.assert_not_called()
        self.assertEqual(spawn.call_args.args[0][-2:], [str(self.base / 'tools/publishing.py'), '--run'])

    def test_watcher_never_spawns_thumbnail_generation(self):
        (self.base / 'tools').mkdir()
        (self.base / 'tools/publishing.py').touch()
        (self.base / 'tools/thumbnail_auto.py').touch()
        with patch.object(sys, 'argv', ['yt_autowatch.py']), patch.object(w, 'paths_lock', return_value=contextlib.nullcontext()), patch.object(w.subprocess, 'Popen') as spawn, patch.object(w.subprocess, 'run') as run:
            w.main()
        run.assert_not_called()
        commands = [c.args[0] for c in spawn.call_args_list]
        self.assertTrue(commands)   # the non-thumbnail workers (metadata/publishing) still start
        self.assertFalse(any('thumbnail' in ' '.join(map(str, c)) for c in commands))
        self.assertFalse(any('thumbnail' in ' '.join(map(str, c.args[0])) for c in run.call_args_list))

    def test_missing_update_token_never_starts_interactive_oauth(self):
        # Use the original function behind the protective mock for this local-only branch.
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location('publishing_auth_test', p.__file__)
        module = module_from_spec(spec); spec.loader.exec_module(module); module.BASE = self.base
        with patch.object(u, 'load_or_create_creds') as interactive:
            with self.assertRaises(u.AuthRequired): module.update_service()
            interactive.assert_not_called()

if __name__ == '__main__': unittest.main()
