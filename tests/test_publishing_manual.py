"""Manual publish workflow: publishing.py --project NAME --dry-run | --apply (human approval = READY file)."""
import base64
import hashlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from tools import publishing as p
import yt_autoupload as u

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1sAAAAASUVORK5CYII=')
SRT = '1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond cue.\n'
META = ('title:\nTown Walk\n\ntitle_alternatives:\nOther Title\n\ndescription:\nA walk through town.\n\n'
        'privacy: unlisted\n\ntags:\n#Travel, #Film\n')
REMOTE = {'id': 'abcdefghijk', 'snippet': {
    'title': 'Old', 'description': 'Old', 'categoryId': '19', 'defaultLanguage': 'en', 'defaultAudioLanguage': 'en',
    'tags': ['old'], 'channelId': 'readonly', 'publishedAt': '2026-10-03T00:00:00Z', 'thumbnails': {}},
    'status': {'privacyStatus': 'unlisted', 'embeddable': False, 'license': 'creativeCommon', 'publicStatsViewable': True,
               'selfDeclaredMadeForKids': True, 'madeForKids': True, 'uploadStatus': 'processed'},
    'recordingDetails': {'location': {'latitude': 1.0, 'longitude': 2.0}}}
SETTINGS = ('PROJECT_ID=EP\nPLAYLIST=My List\nTITLE_DESCRIPTION_LANGUAGE=fr\nCATEGORY=Travel & Events\n'
            'MADE_FOR_KIDS=false\nLICENSE=standard\nEMBEDDING_ALLOWED=true\nAI_DISCLOSURE=false\n'
            'AUTO_CHAPTERS=true\nCOMMENTS_ENABLED=true\nVIDEO_LANGUAGE=en\nPAID_PROMOTION=false\nRECORDING_DATE=\n')


class FakeCreds:
    def __init__(self, scopes=None):
        self.scopes = scopes or ['https://www.googleapis.com/auth/youtube.force-ssl']
        self.expired, self.valid, self.refresh_token = False, True, 'r'
    def has_scopes(self, wanted):
        return all(w in self.scopes for w in wanted)


class ManualPublishTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        for name in ('done', 'logs', 'secrets'):
            (self.base / name).mkdir()
        self.folder = self.base / 'done' / 'EP'
        self.make_assets()
        self.patch(p, 'BASE', self.base)
        self.patch(u, 'BASE', self.base)
        # fail closed: nothing may reach real auth / network / upload
        self.auth = self.patch(p, 'update_service', MagicMock(side_effect=AssertionError('LIVE AUTH FORBIDDEN')))
        for name in ('get_upload_service', 'upload_one', 'make_upload_request', 'get_check_service'):
            self.patch(u, name, MagicMock(side_effect=AssertionError(f'{name} FORBIDDEN (no video upload)')))
        self.service = MagicMock()
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': [json.loads(json.dumps(REMOTE))]}
        self.service.videos.return_value.update.return_value.execute.return_value = {'id': 'abcdefghijk'}
        self.service.thumbnails.return_value.set.return_value.execute.return_value = {'items': [{'default': {'url': 'x'}}]}
        self.captions = self.service.captions.return_value
        self.captions.list.return_value.execute.return_value = {'items': []}
        self.captions.insert.return_value.execute.return_value = {'id': 'cap1'}
        self.captions.update.return_value.execute.return_value = {'id': 'cap1'}
        self.playlists = self.service.playlists.return_value
        self.playlists.list.return_value.execute.return_value = {'items': [{'id': 'PL1', 'snippet': {'title': 'My List'}}]}
        self.items = self.service.playlistItems.return_value
        self.items.list.return_value.execute.return_value = {'items': []}
        self.items.insert.return_value.execute.return_value = {'id': 'PLI1'}
        self.lines = []

    def make_assets(self):
        self.folder.mkdir()
        (self.folder / 'meta.txt').write_text(META)
        (self.folder / 'youtube_video_id.txt').write_text('abcdefghijk')
        (self.folder / 'Thumbnail.png').write_bytes(PNG)
        (self.folder / 'READY').touch()
        (self.folder / 'subs').mkdir()
        (self.folder / 'subs/final.srt').write_text(SRT)

    def fresh_project(self):
        import shutil
        shutil.rmtree(self.folder)
        self.make_assets()
        self.service.reset_mock()

    def patch(self, obj, name, value):
        item = patch.object(obj, name, value); item.start(); self.addCleanup(item.stop)
        return value

    def run_manual(self, apply=False, service=None):
        return p.manual_run(self.folder, apply=apply, service=service, out=self.lines.append)

    def text(self):
        return '\n'.join(self.lines)

    def snapshot_files(self):
        return {str(f.relative_to(self.folder)): f.read_bytes() for f in self.folder.rglob('*') if f.is_file()}

    def assert_no_youtube_activity(self):
        self.auth.assert_not_called()
        self.assertEqual(self.service.mock_calls, [])

    # --- READY contract -------------------------------------------------------
    def test_missing_ready_blocks_dry_run_and_apply_without_any_write(self):
        (self.folder / 'READY').unlink()
        for apply in (False, True):
            self.lines.clear()
            self.assertEqual(self.run_manual(apply, self.service), 1)
            self.assertIn('READY: no', self.text())
            self.assertIn('READY approval marker absent', self.text())
        self.assert_no_youtube_activity()
        self.assertFalse((self.folder / '.applied').exists())

    def test_ready_filename_is_exactly_READY(self):
        (self.folder / 'READY').unlink()
        for wrong in ('.ready', 'READY.txt', 'ALL_READY', '.READY', 'READY.md', '.approved', 'ready'):
            (self.folder / wrong).touch()      # note: on case-insensitive APFS `ready` is a different entry name than READY
        self.assertEqual(self.run_manual(True, self.service), 1)
        self.assertIn('READY: no', self.text())

    def test_missing_or_invalid_meta_only_affects_the_metadata_group(self):
        for content in ('', 'title:\n\ndescription:\nx\n', 'title:\nT\n\ndescription:\n\n',
                        'title:\nBad <b>\n\ndescription:\nx\n', 'title:\n' + 'T' * 101 + '\n\ndescription:\nx\n',
                        'title:\nT\n\ndescription:\nok > not ok\n'):
            with self.subTest(content=content):
                self.fresh_project()
                (self.folder / 'meta.txt').write_text(content)
                self.lines.clear()
                self.assertEqual(self.run_manual(True, self.service), 4)      # applied the rest, skipped the invalid group
                self.assertIn('METADATA: INVALID', self.text()); self.assertIn('meta.txt invalid', self.text())
                self.service.videos.return_value.update.assert_not_called()
                self.service.thumbnails.return_value.set.assert_called_once()
                self.captions.insert.assert_called_once()

    def test_missing_metadata_does_not_block_other_groups(self):
        (self.folder / 'meta.txt').unlink()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('METADATA: MISSING', self.text())
        self.service.videos.return_value.update.assert_not_called()
        self.service.thumbnails.return_value.set.assert_called_once()
        self.captions.insert.assert_called_once()

    def test_missing_or_invalid_video_id_blocks(self):
        for content in (None, '', 'short', 'has spaces!!', 'abcdefghijkl'):
            with self.subTest(content=content):
                self.lines.clear()
                if content is None:
                    (self.folder / 'youtube_video_id.txt').unlink()
                else:
                    (self.folder / 'youtube_video_id.txt').write_text(content)
                self.assertEqual(self.run_manual(True, self.service), 1)
                self.assertIn('video ID missing/invalid', self.text())
        self.assert_no_youtube_activity()

    def test_missing_thumbnail_does_not_block_other_groups(self):
        (self.folder / 'Thumbnail.png').unlink()
        (self.folder / 'cover.png').write_bytes(PNG)            # not named like a thumbnail
        (self.folder / '.thumbnail_hidden.png').write_bytes(PNG)  # hidden files are ignored
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('THUMBNAIL: MISSING', self.text()); self.assertIn('thumbnail missing', self.text())
        self.service.thumbnails.return_value.set.assert_not_called()
        self.service.videos.return_value.update.assert_called_once()
        self.captions.insert.assert_called_once()

    def test_ambiguous_thumbnail_is_not_applied_and_names_the_files(self):
        for extra in ('thumbnail.jpg', 'Thumbnail - 2.png', 'THUMBNAIL_final.PNG'):
            with self.subTest(extra=extra):
                self.fresh_project()
                (self.folder / extra).write_bytes(PNG)
                self.lines.clear()
                self.assertEqual(self.run_manual(True, self.service), 4)
                self.assertIn('THUMBNAIL: AMBIGUOUS', self.text())
                self.assertIn('thumbnail ambiguous', self.text()); self.assertIn(extra, self.text())
                self.service.thumbnails.return_value.set.assert_not_called()
                self.service.videos.return_value.update.assert_called_once()   # the other groups still synchronized

    def test_unsupported_or_invalid_thumbnail_is_invalid_but_independent(self):
        (self.folder / 'Thumbnail.png').unlink()
        (self.folder / 'thumbnail.webp').write_bytes(PNG)
        self.assertEqual(self.run_manual(True, self.service), 4)
        self.assertIn('only JPEG/PNG', self.text()); self.assertIn('THUMBNAIL: INVALID', self.text())
        (self.folder / 'thumbnail.webp').unlink()
        (self.folder / 'thumbnail.png').write_bytes(b'not an image')
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 4)
        self.assertRegex(self.text(), r'not a decodable|invalid image dimensions')
        self.service.thumbnails.return_value.set.assert_not_called()

    def test_a_single_thumbnail_is_selected_and_shown_with_exact_path(self):
        self.assertEqual(self.run_manual(False), 0)
        self.assertIn(f'path: {self.folder / "Thumbnail.png"}', self.text())

    def test_missing_oauth_credentials_stop_without_writing(self):
        self.auth.side_effect = u.AuthRequired('Update OAuth required: run publishing.py --authorize interactively')
        self.assertEqual(p.manual_run(self.folder, apply=True, out=self.lines.append), 1)
        self.assertIn('Update OAuth required', self.text())
        self.assertIn('Nothing was sent to YouTube', self.text())
        self.assertFalse((self.folder / '.applied').exists())

    # --- dry run ----------------------------------------------------------------
    def test_dry_run_performs_zero_writes_and_prints_the_preflight_report(self):
        before = self.snapshot_files()
        self.assertEqual(self.run_manual(False, self.service), 0)
        for expected in ('PROJECT: EP', 'VIDEO_ID: abcdefghijk', 'title: Town Walk', 'description: 20 chars', 'tags: 2',
                         'METADATA: READY', 'THUMBNAIL: READY', 'SUBTITLE: READY', 'YOUTUBE_SETTINGS: MISSING', 'subs/final.srt',
                         'language: en', 'READY: yes', 'WOULD APPLY:', '- metadata: ready', '- thumbnail: ready', '- subtitles: ready',
                         'MISSING (not applied):', '- youtube_settings:', 'DRY RUN OK'):
            self.assertIn(expected, self.text())
        self.assertEqual(self.snapshot_files(), before)           # no file created, none modified
        self.assert_no_youtube_activity()                         # no credentials, no API
        self.assertFalse((self.folder / '.applied').exists())

    # --- apply ------------------------------------------------------------------
    def test_apply_cannot_reupload_the_video(self):
        self.assertEqual(self.run_manual(True, self.service), 0)
        names = {c[0] for c in self.service.mock_calls}
        self.assertTrue({'videos', 'thumbnails'} <= names)
        self.assertFalse(any('videos().insert' in str(c) for c in self.service.mock_calls))   # video bytes are never uploaded
        self.assertFalse(any('delete' in str(c) for c in self.service.mock_calls))             # nothing is ever deleted
        source = inspect.getsource(p.manual_run) + inspect.getsource(p.manual_package) + inspect.getsource(p.build_snippet)
        for forbidden in ('.insert(', 'videos().insert', 'upload_one', 'make_upload_request', 'resumable', 'get_upload_service', 'IRN1.mov', '.mov'):
            self.assertNotIn(forbidden, source)
        media = [c for c in self.service.mock_calls if 'MediaFileUpload' in str(c)]
        self.assertTrue(media and all(str(c).startswith(('call.thumbnails().set', 'call.captions().insert')) for c in media))  # media only goes to thumbnails.set / captions.insert (image + srt)
        self.assertFalse(any('videos' in str(c) and 'MediaFileUpload' in str(c) for c in self.service.mock_calls))

    def test_unrelated_youtube_settings_are_preserved(self):
        self.assertEqual(self.run_manual(True, self.service), 0)
        update = self.service.videos.return_value.update.call_args.kwargs
        self.assertEqual(update['part'], 'snippet')                       # not status / recordingDetails / localizations
        body = update['body']
        self.assertEqual(set(body), {'id', 'snippet'})                    # no status/privacy/madeForKids/etc.
        snippet = body['snippet']
        self.assertEqual((snippet['title'], snippet['description']), ('Town Walk', 'A walk through town.'))
        self.assertEqual(snippet['categoryId'], '19')
        self.assertEqual((snippet['defaultLanguage'], snippet['defaultAudioLanguage']), ('en', 'en'))
        self.assertEqual(snippet['tags'], ['#Travel', '#Film'])
        self.assertNotIn('channelId', snippet); self.assertNotIn('publishedAt', snippet)
        thumb = self.service.thumbnails.return_value.set.call_args.kwargs
        self.assertEqual(thumb['videoId'], 'abcdefghijk')                 # targets the existing video

    def test_remote_tags_are_kept_when_meta_has_no_tags(self):
        (self.folder / 'meta.txt').write_text('title:\nTown Walk\n\ndescription:\nA walk.\n')
        self.assertEqual(self.run_manual(True, self.service), 0)
        snippet = self.service.videos.return_value.update.call_args.kwargs['body']['snippet']
        self.assertEqual(snippet['tags'], ['old'])
        self.assertIn('tags: absent', self.text())

    def test_video_not_visible_to_credentials_fails_before_any_write(self):
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': []}
        self.assertEqual(self.run_manual(True, self.service), 2)
        self.service.videos.return_value.update.assert_not_called()
        self.service.thumbnails.return_value.set.assert_not_called()
        self.assertFalse((self.folder / '.applied').exists())

    def test_applied_marker_created_only_after_complete_success_with_hashes(self):
        before = self.snapshot_files()
        self.assertEqual(self.run_manual(True, self.service), 0)
        record = json.loads((self.folder / '.applied').read_text())
        self.assertEqual(record['video_id'], 'abcdefghijk')
        self.assertEqual(record['meta_sha256'], hashlib.sha256(META.encode()).hexdigest())
        self.assertEqual(record['thumbnail_sha256'], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(record['thumbnail'], 'Thumbnail.png')
        self.assertEqual(record['subtitle_sha256'], hashlib.sha256(SRT.encode()).hexdigest())
        self.assertEqual((record['subtitle_language'], record['caption_id']), ('en', 'cap1'))
        self.assertIn('applied_at', record)
        self.assertTrue((self.folder / 'READY').exists())               # .ready is never deleted
        after = self.snapshot_files()
        for name in ('meta.txt', 'Thumbnail.png', 'youtube_video_id.txt', 'subs/final.srt'):
            self.assertEqual(after[name], before[name])                  # source assets untouched

    def test_second_apply_is_a_noop_and_skips_each_group(self):
        self.run_manual(True, self.service)
        calls = len(self.service.mock_calls)
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('NOTHING TO APPLY', self.text())
        for group in ('METADATA', 'THUMBNAIL', 'SUBTITLE'):
            self.assertIn(f'{group}: ALREADY_APPLIED', self.text())
        self.assertIn('- metadata: already applied', self.text())
        self.assertEqual(len(self.service.mock_calls), calls)

    def test_changed_metadata_after_apply_is_detected_and_reapplies_only_metadata(self):
        self.run_manual(True, self.service)
        (self.folder / 'meta.txt').write_text(META.replace('Town Walk', 'New Title'))
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('METADATA: CHANGED', self.text())
        self.assertEqual(self.service.videos.return_value.update.call_count, 2)
        self.assertEqual(self.service.thumbnails.return_value.set.call_count, 1)    # unchanged groups skipped
        self.assertEqual(self.captions.insert.call_count, 1)
        self.assertEqual(self.captions.update.call_count, 0)

    def test_changed_thumbnail_does_not_reapply_metadata_or_subtitles(self):
        self.run_manual(True, self.service)
        (self.folder / 'Thumbnail.png').write_bytes(PNG + b'\x00')
        with patch.object(p, 'validate_image'):
            self.lines.clear()
            self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('THUMBNAIL: CHANGED', self.text())
        self.assertEqual(self.service.thumbnails.return_value.set.call_count, 2)
        self.assertEqual(self.service.videos.return_value.update.call_count, 1)
        self.assertEqual(self.captions.insert.call_count + self.captions.update.call_count, 1)

    def test_asset_added_after_a_previous_applied_marker_is_applied_on_the_same_command(self):
        (self.folder / 'Thumbnail.png').unlink()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertTrue((self.folder / '.applied').exists())           # .applied exists but does not mean "done forever"
        self.service.thumbnails.return_value.set.assert_not_called()
        (self.folder / 'Thumbnail.png').write_bytes(PNG)
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('OK thumbnail', self.text())
        self.service.thumbnails.return_value.set.assert_called_once()
        self.assertEqual(self.service.videos.return_value.update.call_count, 1)    # metadata not re-sent
        self.assertEqual(self.captions.insert.call_count, 1)
        self.assertIsNotNone(json.loads((self.folder / '.applied').read_text())['groups']['thumbnail'])

    def test_partial_failure_keeps_successful_per_asset_state_and_retry_repeats_only_the_failure(self):
        thumbs = self.service.thumbnails.return_value.set.return_value.execute
        thumbs.side_effect = RuntimeError('HTTP 500 backend error')
        self.assertEqual(self.run_manual(True, self.service), 2)
        self.assertIn('PARTIAL FAILURE', self.text())
        self.assertIn('never re-uploaded', self.text())
        groups = json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']
        self.assertEqual(set(groups), {'metadata', 'subtitle'})          # independent groups succeeded and are recorded
        self.assertEqual(self.service.videos.return_value.update.call_count, 1)
        self.assertEqual(self.captions.insert.call_count, 1)
        thumbs.side_effect = None
        thumbs.return_value = {'items': [{'default': {'url': 'x'}}]}
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertEqual(self.service.videos.return_value.update.call_count, 1)   # metadata NOT sent again
        self.assertEqual(self.captions.insert.call_count, 1)
        self.assertEqual(thumbs.call_count, 2)
        self.assertEqual(set(json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']),
                         {'metadata', 'thumbnail', 'subtitle'})

    def test_metadata_failure_does_not_block_the_independent_groups(self):
        self.service.videos.return_value.update.return_value.execute.side_effect = RuntimeError('HTTP 403')
        self.assertEqual(self.run_manual(True, self.service), 2)
        self.assertIn('FAILED metadata', self.text())
        self.service.thumbnails.return_value.set.assert_called_once()
        self.captions.insert.assert_called_once()
        self.assertNotIn('metadata', json.loads((self.folder / '.manual_apply_state.json').read_text())['groups'])

    def test_group_changed_mid_run_is_not_applied(self):
        def change(*a, **k):
            (self.folder / 'meta.txt').write_text(META.replace('Town Walk', 'Edited Meanwhile'))
            return {'id': 'abcdefghijk'}
        # the thumbnail group is checked right before its write; change the thumbnail while metadata is being sent
        def change_thumb(*a, **k):
            (self.folder / 'Thumbnail.png').write_bytes(PNG + b'\x00')
            return {'id': 'abcdefghijk'}
        self.service.videos.return_value.update.return_value.execute.side_effect = change_thumb
        with patch.object(p, 'validate_image'):
            self.assertEqual(self.run_manual(True, self.service), 2)
        self.assertIn('PARTIAL FAILURE', self.text())
        self.assertIn('thumbnail changed during the run', self.text())
        self.service.thumbnails.return_value.set.assert_not_called()
        self.captions.insert.assert_called_once()                         # the unaffected group still went through

    def test_uploader_busy_stops_without_writing(self):
        import fcntl
        with (self.base / '.yt_autoupload.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.run_manual(True, self.service), 3)
        self.assert_no_youtube_activity()

    # --- final.srt as the English caption track ---------------------------------------
    def test_missing_final_srt_does_not_block_other_groups(self):
        (self.folder / 'subs/final.srt').unlink()
        self.assertEqual(self.run_manual(False, self.service), 0)
        self.assertIn('SUBTITLE: MISSING', self.text())
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.service.videos.return_value.update.assert_called_once()
        self.service.thumbnails.return_value.set.assert_called_once()
        self.assertEqual(self.captions.mock_calls, [])

    def test_invalid_final_srt_is_not_applied_but_others_are(self):
        for content in ('', 'not an srt', '1\n00:00:05,000 --> 00:00:01,000\nbackwards\n', '1\n00:00:01,000 --> 00:00:02,000\n'):
            with self.subTest(content=content):
                self.fresh_project()
                (self.folder / 'subs/final.srt').write_text(content)
                self.lines.clear()
                self.assertEqual(self.run_manual(True, self.service), 4)
                self.assertIn('SUBTITLE: INVALID', self.text()); self.assertIn('final.srt invalid', self.text())
                self.assertEqual(self.captions.mock_calls, [])
                self.service.thumbnails.return_value.set.assert_called_once()

    def test_only_final_srt_is_the_subtitle_source(self):
        for name in ('en.srt', 'hybrid.srt', 'youtube.srt'):
            (self.folder / 'subs' / name).write_text(SRT.replace('Hello there', 'WRONG SOURCE ' + name))
        (self.folder / 'subs/final.srt').unlink()
        self.assertEqual(self.run_manual(True, self.service), 0)          # other subtitle files never substitute
        self.assertIn('SUBTITLE: MISSING', self.text())
        self.assertEqual(self.captions.mock_calls, [])
        (self.folder / 'subs/final.srt').write_text(SRT)
        sent = []
        def capture(path, **kwargs):
            sent.append((Path(path).name, Path(path).read_bytes(), kwargs.get('mimetype')))
            return MagicMock()
        self.lines.clear()
        with patch.object(u, 'MediaFileUpload', side_effect=capture):
            self.assertEqual(self.run_manual(True, self.service), 0)
        caption = [item for item in sent if item[0] == 'final.srt']
        self.assertEqual(len(caption), 1)
        self.assertEqual(caption[0][1], SRT.encode())                      # exactly final.srt's bytes
        self.assertEqual((self.folder / 'subs/final.srt').read_text(), SRT)   # never modified

    def test_dry_run_uploads_no_caption(self):
        self.assertEqual(self.run_manual(False, self.service), 0)
        self.assertEqual(self.captions.mock_calls, [])
        self.assert_no_youtube_activity()

    def test_apply_inserts_one_english_standard_track(self):
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.captions.list.assert_called_once()
        self.captions.insert.assert_called_once()
        snippet = self.captions.insert.call_args.kwargs['body']['snippet']
        self.assertEqual((snippet['videoId'], snippet['language'], snippet['isDraft']), ('abcdefghijk', 'en', False))
        self.captions.update.assert_not_called()

    def test_caption_failure_keeps_other_groups_recorded_and_retries_only_the_caption(self):
        self.captions.insert.return_value.execute.side_effect = RuntimeError('HTTP 403 forbidden')
        self.assertEqual(self.run_manual(True, self.service), 2)
        self.assertIn('PARTIAL FAILURE', self.text())
        self.assertIn('metadata', self.text()); self.assertIn('subtitles', self.text())
        state = json.loads((self.folder / '.manual_apply_state.json').read_text())
        self.assertEqual(set(state['groups']), {'metadata', 'thumbnail'})
        self.captions.insert.return_value.execute.side_effect = None
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertEqual(self.service.videos.return_value.update.call_count, 1)
        self.assertEqual(self.service.thumbnails.return_value.set.call_count, 1)
        self.assertEqual(self.captions.insert.call_count, 2)
        self.assertEqual(set(json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']),
                         {'metadata', 'thumbnail', 'subtitle'})

    def test_retry_after_interrupted_insert_adopts_the_track_instead_of_duplicating(self):
        self.captions.insert.return_value.execute.side_effect = TimeoutError('timed out after the server created the track')
        self.assertEqual(self.run_manual(True, self.service), 2)
        state = json.loads((self.folder / '.manual_apply_state.json').read_text())
        self.assertTrue(state['subtitle_insert_started'])
        # the track DID get created server-side; retry must find and update it, never insert a second one
        self.captions.insert.return_value.execute.side_effect = None
        self.captions.list.return_value.execute.return_value = {'items': [
            {'id': 'created-by-us', 'snippet': {'language': 'en', 'trackKind': 'standard', 'name': ''}}]}
        self.captions.update.return_value.execute.return_value = {'id': 'created-by-us'}
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertEqual(self.captions.insert.call_count, 1)
        self.captions.update.assert_called_once()
        self.assertEqual(self.captions.update.call_args.kwargs['body']['id'], 'created-by-us')
        self.assertEqual(json.loads((self.folder / '.applied').read_text())['caption_id'], 'created-by-us')

    def test_changed_final_srt_updates_our_recorded_track_in_place(self):
        self.assertEqual(self.run_manual(True, self.service), 0)
        (self.folder / 'subs/final.srt').write_text(SRT.replace('Hello there', 'Hello again'))
        self.captions.list.return_value.execute.return_value = {'items': [
            {'id': 'cap1', 'snippet': {'language': 'en', 'trackKind': 'standard'}}]}
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertEqual(self.captions.insert.call_count, 1)               # still just one track ever created
        self.assertEqual(self.captions.update.call_args.kwargs['body']['id'], 'cap1')

    def test_unrelated_caption_tracks_are_preserved(self):
        self.captions.list.return_value.execute.return_value = {'items': [
            {'id': 'es', 'snippet': {'language': 'es', 'trackKind': 'standard'}},
            {'id': 'fr', 'snippet': {'language': 'fr', 'trackKind': 'standard'}},
            {'id': 'asr', 'snippet': {'language': 'en', 'trackKind': 'asr'}},
            {'id': 'forced', 'snippet': {'language': 'en', 'trackKind': 'forced'}}]}
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.captions.insert.assert_called_once()                          # ASR/forced/other languages do not count as duplicates
        self.captions.update.assert_not_called()
        self.assertFalse(any('delete' in str(c) for c in self.captions.mock_calls))

    def test_existing_unknown_standard_english_track_is_not_overwritten_or_duplicated(self):
        self.captions.list.return_value.execute.return_value = {'items': [
            {'id': 'human', 'snippet': {'language': 'en', 'trackKind': 'standard', 'name': 'Mine', 'lastUpdated': '2026-10-01'}}]}
        self.assertEqual(self.run_manual(True, self.service), 2)
        self.assertIn('already exists', self.text()); self.assertIn('id=human', self.text())
        self.assertIn('--replace-english-caption', self.text())
        self.captions.insert.assert_not_called(); self.captions.update.assert_not_called()
        # explicit opt-in replaces that single track's content
        self.lines.clear()
        self.assertEqual(p.manual_run(self.folder, apply=True, service=self.service, out=self.lines.append, replace_english=True), 0)
        self.captions.update.assert_called_once()
        self.assertEqual(self.captions.update.call_args.kwargs['body']['id'], 'human')
        self.captions.insert.assert_not_called()

    def test_replace_flag_refuses_when_several_english_tracks_exist(self):
        self.captions.list.return_value.execute.return_value = {'items': [
            {'id': 'a', 'snippet': {'language': 'en', 'trackKind': 'standard'}},
            {'id': 'b', 'snippet': {'language': 'en', 'trackKind': 'standard'}}]}
        self.assertEqual(p.manual_run(self.folder, apply=True, service=self.service, out=self.lines.append, replace_english=True), 2)
        self.captions.update.assert_not_called(); self.captions.insert.assert_not_called()

    def test_rerun_with_lost_applied_marker_makes_no_new_api_writes(self):
        self.assertEqual(self.run_manual(True, self.service), 0)
        (self.folder / '.applied').unlink()
        writes = len(self.service.mock_calls)
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertEqual(self.captions.insert.call_count, 1)
        self.assertEqual(self.service.videos.return_value.update.call_count, 1)

    # --- credentials: reuse the existing caption token -----------------------------
    def test_manual_service_reuses_the_existing_caption_token_without_new_oauth(self):
        self.patch(p, 'update_service', MagicMock(side_effect=AssertionError('update token absent: must not be used')))
        with self.assertRaises(u.AuthRequired):
            p.manual_service()                                              # neither token present
        (self.base / 'secrets/token_caption.pickle').write_bytes(__import__('pickle').dumps(FakeCreds()))
        built = {}
        with patch.object(u, 'build', side_effect=lambda *a, **kw: built.update(kw) or 'SERVICE'):
            self.assertEqual(p.manual_service(), 'SERVICE')
        self.assertIsInstance(built['credentials'], FakeCreds)
        (self.base / 'secrets/token_caption.pickle').write_bytes(__import__('pickle').dumps(FakeCreds(scopes=['other'])))
        with self.assertRaisesRegex(u.AuthRequired, 'force-ssl'):
            p.manual_service()

    # --- CLI --------------------------------------------------------------------
    def test_cli_requires_exactly_one_of_dry_run_or_apply_and_never_enables_watchers(self):
        for argv in (['--project', 'EP'], ['--project', 'EP', '--run'], ['--project', 'EP', '--dry-run', '--apply'],
                     ['--project', 'EP', '--authorize']):
            with self.subTest(argv=argv), patch.object(sys, 'argv', ['publishing.py'] + argv), \
                 patch('sys.stderr'), self.assertRaises(SystemExit) as ctx:
                p.main()
            self.assertEqual(ctx.exception.code, 2)
        with patch.object(sys, 'argv', ['publishing.py', '--project', 'EP', '--dry-run']), \
             patch.object(p, 'manual_run', return_value=0) as run, self.assertRaises(SystemExit) as ctx:
            p.main()
        self.assertEqual(run.call_args.args[0], self.folder)
        self.assertFalse(run.call_args.kwargs['apply'])
        self.assertFalse(run.call_args.kwargs['replace_english'])
        self.assertFalse((self.base / '.publishing_updates_enabled').exists())

    def test_existing_automatic_publishing_path_is_unchanged_and_ignores_ready(self):
        # `process()` (watcher worker) still only records readiness locally; the READY marker is not part of it.
        self.assertEqual(p.snapshot.__name__, 'snapshot')
        for name in (self.folder / 'READY',):
            name.unlink()
        os.utime(self.folder / 'meta.txt', (1, 1)); os.utime(self.folder / 'Thumbnail.png', (1, 1))
        os.utime(self.folder / 'youtube_video_id.txt', (1, 1))
        self.assertIn('PACKAGE VALID', p.process(self.folder))   # read-only mode, no READY needed


    # --- youtube_settings.txt + incremental synchronization ----------------------------------------
    def settings_update_calls(self):
        return [c for c in self.service.videos.return_value.update.call_args_list if c.kwargs['part'] != 'snippet']

    def test_missing_settings_file_does_not_block_other_groups(self):
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('YOUTUBE_SETTINGS: MISSING', self.text())
        self.assertEqual(self.settings_update_calls(), [])
        self.items.insert.assert_not_called()

    def test_settings_only_second_pass_skips_the_three_applied_groups(self):
        self.assertEqual(self.run_manual(True, self.service), 0)
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS)
        self.lines.clear()
        self.assertEqual(self.run_manual(False, self.service), 0)
        self.assertIn('- youtube_settings: ready fields', self.text())
        for line in ('- metadata: already applied', '- thumbnail: already applied', '- subtitles: already applied'):
            self.assertIn(line, self.text())
        before = (self.service.videos.return_value.update.call_count, self.service.thumbnails.return_value.set.call_count,
                  self.captions.insert.call_count)
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertEqual(self.service.videos.return_value.update.call_count, before[0] + 1)   # exactly one settings update
        self.assertEqual(self.service.thumbnails.return_value.set.call_count, before[1])
        self.assertEqual(self.captions.insert.call_count, before[2])
        self.items.insert.assert_called_once()
        self.assertEqual(self.items.insert.call_args.kwargs['body']['snippet'],
                         {'playlistId': 'PL1', 'resourceId': {'kind': 'youtube#video', 'videoId': 'abcdefghijk'}})

    def test_settings_update_starts_from_the_live_video_and_does_not_clear_unrelated_fields(self):
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS)
        (self.folder / 'meta.txt').unlink()
        self.run_manual(True, self.service)
        call = self.settings_update_calls()[0]
        self.assertEqual(call.kwargs['part'], 'snippet,status')              # recordingDetails not touched (blank date)
        body = call.kwargs['body']
        self.assertEqual(set(body), {'id', 'snippet', 'status'})
        snippet, status = body['snippet'], body['status']
        self.assertEqual((snippet['title'], snippet['description'], snippet['tags'], snippet['categoryId'],
                          snippet['defaultAudioLanguage']), ('Old', 'Old', ['old'], '19', 'en'))   # live values carried over
        self.assertEqual(snippet['defaultLanguage'], 'fr')
        self.assertNotIn('channelId', snippet); self.assertNotIn('publishedAt', snippet)
        self.assertEqual(status, {'privacyStatus': 'unlisted', 'embeddable': True, 'license': 'youtube',
                                  'publicStatsViewable': True, 'selfDeclaredMadeForKids': False})
        # privacy/public stats kept; read-only fields dropped; AI_DISCLOSURE=false equals the live (absent) value => not written

    def test_only_status_is_sent_when_snippet_values_already_match_live(self):
        (self.folder / 'youtube_settings.txt').write_text('TITLE_DESCRIPTION_LANGUAGE=en\nCATEGORY=19\nLICENSE=standard\n')
        self.run_manual(True, self.service)
        call = self.settings_update_calls()[0]
        self.assertEqual(call.kwargs['part'], 'status')
        self.assertEqual(set(call.kwargs['body']), {'id', 'status'})
        state = json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']['settings']['fields']
        self.assertEqual((state['CATEGORY']['wrote'], state['LICENSE']['wrote']), (False, True))   # synced without a write

    def test_nothing_is_written_when_live_already_matches_the_settings(self):
        live = json.loads(json.dumps(REMOTE)); live['snippet']['defaultLanguage'] = 'fr'
        live['status'].update(embeddable=True, license='youtube', selfDeclaredMadeForKids=False)
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': [live]}
        (self.folder / 'youtube_settings.txt').write_text(
            'TITLE_DESCRIPTION_LANGUAGE=fr\nMADE_FOR_KIDS=false\nLICENSE=standard\nEMBEDDING_ALLOWED=true\nAI_DISCLOSURE=false\n')
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertEqual(self.settings_update_calls(), [])
        self.assertEqual(json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']['settings']['fields']['LICENSE']['wrote'], False)

    def test_unsupported_settings_do_not_block_supported_ones_and_are_clearly_reported(self):
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS)
        raw = (self.folder / 'youtube_settings.txt').read_bytes()
        self.run_manual(False, self.service)
        for key, status in (('AUTO_CHAPTERS', 'NOT_EXPOSED'), ('COMMENTS_ENABLED', 'NOT_EXPOSED'), ('VIDEO_LANGUAGE', 'NOT_EXPOSED'),
                            ('PAID_PROMOTION', 'MANUAL'), ('RECORDING_DATE', 'BLANK')):
            self.assertRegex(self.text(), rf'- {key}: {status}')
        self.assertIn('MANUAL / UNSUPPORTED', self.text())
        self.assert_no_youtube_activity()
        self.lines.clear()
        self.run_manual(True, self.service)
        applied = json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']['settings']['fields']
        self.assertEqual(set(applied), {'PLAYLIST', 'TITLE_DESCRIPTION_LANGUAGE', 'CATEGORY', 'MADE_FOR_KIDS', 'LICENSE',
                                        'EMBEDDING_ALLOWED', 'AI_DISCLOSURE'})
        self.assertEqual((self.folder / 'youtube_settings.txt').read_bytes(), raw)   # unsupported lines preserved verbatim

    def test_changing_one_setting_reapplies_only_that_setting(self):
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS)
        self.run_manual(True, self.service)
        updates, inserts, lists = (len(self.settings_update_calls()), self.items.insert.call_count, self.playlists.list.call_count)
        live = json.loads(json.dumps(REMOTE)); live['status']['license'] = 'youtube'   # what the first apply produced
        self.service.videos.return_value.list.return_value.execute.return_value = {'items': [live]}
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS.replace('LICENSE=standard', 'LICENSE=creative_commons'))
        self.lines.clear()
        self.run_manual(False, self.service)
        self.assertIn('YOUTUBE_SETTINGS: CHANGED', self.text())
        self.run_manual(True, self.service)
        self.assertEqual(len(self.settings_update_calls()), updates + 1)
        last = self.settings_update_calls()[-1]
        self.assertEqual(last.kwargs['part'], 'status')                       # snippet values were not re-sent
        self.assertEqual(last.kwargs['body']['status']['license'], 'creativeCommon')
        self.assertEqual((self.items.insert.call_count, self.playlists.list.call_count), (inserts, lists))
        self.lines.clear()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.assertIn('NOTHING TO APPLY', self.text())

    def test_comments_and_unrelated_edits_in_the_settings_file_do_not_reapply(self):
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS)
        self.run_manual(True, self.service)
        calls = len(self.service.mock_calls)
        (self.folder / 'youtube_settings.txt').write_text('# new comment\n' + SETTINGS.replace('AUTO_CHAPTERS=true', 'AUTO_CHAPTERS=false'))
        self.lines.clear()
        self.run_manual(True, self.service)
        self.assertIn('NOTHING TO APPLY', self.text())
        self.assertEqual(len(self.service.mock_calls), calls)

    def test_invalid_setting_values_are_skipped_individually(self):
        (self.folder / 'youtube_settings.txt').write_text('MADE_FOR_KIDS=maybe\nLICENSE=standard\nCATEGORY=Unknown Cat\nLICENSE=x\n')
        self.assertEqual(self.run_manual(True, self.service), 4)
        self.assertIn('MADE_FOR_KIDS=maybe: INVALID', self.text())
        self.assertIn('CATEGORY=Unknown Cat: INVALID', self.text())
        self.assertIn('LICENSE=standard: AMBIGUOUS', self.text())              # duplicate key refused entirely
        self.assertEqual(self.settings_update_calls(), [])

    def test_settings_of_another_project_are_refused_but_other_groups_apply(self):
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS.replace('PROJECT_ID=EP', 'PROJECT_ID=OTHER'))
        self.assertEqual(self.run_manual(True, self.service), 4)
        self.assertIn('YOUTUBE_SETTINGS: INVALID', self.text())
        self.assertEqual(self.settings_update_calls(), [])
        self.service.thumbnails.return_value.set.assert_called_once()

    def test_category_conflicting_with_meta_categoryid_is_ambiguous(self):
        (self.folder / 'meta.txt').write_text(META + '\ncategoryid: 22\n')
        (self.folder / 'youtube_settings.txt').write_text('CATEGORY=Travel & Events\nLICENSE=standard\n')
        self.run_manual(True, self.service)
        self.assertIn('CATEGORY=Travel & Events: AMBIGUOUS', self.text())
        self.assertEqual(set(json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']['settings']['fields']), {'LICENSE'})

    def test_playlist_is_never_created_and_missing_or_duplicate_titles_are_reported(self):
        (self.folder / 'youtube_settings.txt').write_text('PLAYLIST=My List\nLICENSE=standard\n')
        for items, word in (([], 'no playlist titled'),
                            ([{'id': 'A', 'snippet': {'title': 'My List'}}, {'id': 'B', 'snippet': {'title': 'My List'}}], '2 playlists')):
            with self.subTest(word=word):
                self.fresh_project()
                (self.folder / 'youtube_settings.txt').write_text('PLAYLIST=My List\nLICENSE=standard\n')
                self.playlists.list.return_value.execute.return_value = {'items': items}
                self.lines.clear()
                self.assertEqual(self.run_manual(True, self.service), 4)
                self.assertIn(word, self.text())
                self.items.insert.assert_not_called()
                self.playlists.insert.assert_not_called()
                self.assertEqual(set(json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']['settings']['fields']), {'LICENSE'})

    def test_playlist_membership_already_present_is_not_inserted_again(self):
        (self.folder / 'youtube_settings.txt').write_text('PLAYLIST=My List\n')
        self.items.list.return_value.execute.return_value = {'items': [{'id': 'EXISTING'}]}
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.items.insert.assert_not_called()
        field = json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']['settings']['fields']['PLAYLIST']
        self.assertEqual((field['playlist_item_id'], field['wrote']), ('EXISTING', False))

    def test_settings_failure_keeps_successful_fields_and_retries_only_the_rest(self):
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS)
        update = self.service.videos.return_value.update.return_value.execute
        calls = {'n': 0}
        def flaky(*a, **k):
            calls['n'] += 1
            if calls['n'] == 2:                                                   # the settings update (1st call = metadata)
                raise RuntimeError('HTTP 500')
            return {'id': 'abcdefghijk'}
        update.side_effect = flaky
        self.assertEqual(self.run_manual(True, self.service), 2)
        fields = json.loads((self.folder / '.manual_apply_state.json').read_text())['groups']['settings']['fields']
        self.assertEqual(set(fields), {'PLAYLIST', 'CATEGORY', 'AI_DISCLOSURE'})   # playlist + fields already in sync; failed ones absent
        self.assertIn('metadata', json.loads((self.folder / '.manual_apply_state.json').read_text())['groups'])
        self.lines.clear()
        self.run_manual(True, self.service)
        self.assertEqual(self.items.insert.call_count, 1)
        self.assertEqual(self.service.videos.return_value.update.call_count, 3)   # metadata once, settings twice
        self.assertEqual(self.service.thumbnails.return_value.set.call_count, 1)

    def test_recording_date_preserves_the_rest_of_recording_details(self):
        (self.folder / 'youtube_settings.txt').write_text('RECORDING_DATE=2026-09-16\n')
        self.run_manual(True, self.service)
        call = self.settings_update_calls()[0]
        self.assertEqual(call.kwargs['part'], 'recordingDetails')
        self.assertEqual(call.kwargs['body']['recordingDetails'],
                         {'location': {'latitude': 1.0, 'longitude': 2.0}, 'recordingDate': '2026-09-16T00:00:00Z'})

    def test_dry_run_and_missing_ready_do_zero_writes_with_settings_present(self):
        (self.folder / 'youtube_settings.txt').write_text(SETTINGS)
        before = self.snapshot_files()
        self.assertEqual(self.run_manual(False, self.service), 0)
        (self.folder / 'READY').unlink()
        before = self.snapshot_files()
        self.assertEqual(self.run_manual(True, self.service), 1)
        self.assertEqual(self.snapshot_files(), before)
        self.assert_no_youtube_activity()

    def test_legacy_v1_applied_state_is_migrated_in_memory_and_new_settings_are_still_applied(self):
        h = lambda b: hashlib.sha256(b).hexdigest()
        legacy = {'applied_at': '2026-10-06T17:05:34+0900', 'video_id': 'abcdefghijk', 'meta_sha256': h(META.encode()),
                  'thumbnail': 'Thumbnail.png', 'thumbnail_sha256': h(PNG), 'subtitle': 'subs/final.srt',
                  'subtitle_language': 'en', 'subtitle_sha256': h(SRT.encode()), 'caption_id': 'cap-old', 'fingerprint': 'x'}
        (self.folder / '.applied').write_text(json.dumps(legacy))
        (self.folder / '.manual_apply_state.json').write_text(json.dumps({'fingerprint': 'x', 'video_id': 'abcdefghijk',
            'metadata_applied': True, 'thumbnail_applied': True, 'subtitle_applied': True, 'caption_id': 'cap-old', 'status': 'APPLIED'}))
        (self.folder / 'youtube_settings.txt').write_text('LICENSE=standard\n')
        before = self.snapshot_files()
        self.assertEqual(self.run_manual(False, self.service), 0)
        for group in ('METADATA', 'THUMBNAIL', 'SUBTITLE'):
            self.assertIn(f'{group}: ALREADY_APPLIED', self.text())
        self.assertIn('YOUTUBE_SETTINGS: READY', self.text())
        self.assertEqual(self.snapshot_files(), before); self.assert_no_youtube_activity()
        self.assertEqual(self.run_manual(True, self.service), 0)
        self.service.thumbnails.return_value.set.assert_not_called()
        self.captions.insert.assert_not_called(); self.captions.update.assert_not_called()
        self.assertEqual(len(self.settings_update_calls()), 1)
        self.assertEqual(self.service.videos.return_value.update.call_count, 1)   # settings only: metadata not re-sent
        record = json.loads((self.folder / '.applied').read_text())
        self.assertEqual((record['applied_at'], record['caption_id']), (legacy['applied_at'], 'cap-old'))
        self.assertEqual(len(list(self.folder.glob('.applied.previous_*'))), 1)            # original v1 record kept
        self.assertEqual(len(list(self.folder.glob('.manual_apply_state.json.v1_*'))), 1)

    def test_the_module_still_has_no_video_upload_path(self):
        source = inspect.getsource(p)
        self.assertNotIn('videos().insert', source)
        for call in self.service.mock_calls:
            self.assertNotIn('videos().insert', str(call))


if __name__ == '__main__':
    unittest.main()
