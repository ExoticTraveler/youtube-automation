"""Deterministic offline tests: hashtag repair, meta.txt -> upload body, OAuth refresh failures.

Everything Google/OpenAI related is mocked; no network, tokens or videos are touched.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import metadata as m
import yt_autoupload as up
from google.auth.exceptions import RefreshError

GOOD = dict(title='A Walk Through Town',
            title_alternatives=['Meeting Local People', 'Life Along the Street', 'An Afternoon on Foot', 'Conversations in Town'],
            description='A walk through town.', tags=['#Travel', '#Documentary', '#Walking'])


class SanitizeTagsTests(unittest.TestCase):
    def test_edge_cases_become_strict_hashtags(self):
        cases = {
            'Travel': '#Travel', 'travel': '#travel', 'solo travel': '#SoloTravel', '#solo travel': '#SoloTravel',
            '#solo-travel': '#SoloTravel', '  #Tag  ': '#Tag', '#Café': '#Cafe',
            '#Tokyo🗼': '#Tokyo', '＃Tag': '#Tag', '#a_b': '#a_b', '#Tag\n': '#Tag',
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(m.sanitize_tags([raw]), [want])

    def test_unusable_entries_are_dropped(self):
        out = m.sanitize_tags(['#', '🌍', None, 5, '#yt:cc=on', '', '#Ok', '#ok', '#OK'])
        self.assertEqual(out, ['#Ok'])

    def test_output_always_matches_strict_form_and_cap(self):
        out = m.sanitize_tags([f'tag {i}-x🌍' for i in range(40)])
        self.assertEqual(len(out), 15)
        for tag in out:
            self.assertRegex(tag, r'^#[A-Za-z0-9_]+\Z')

    def test_non_list_passes_through_for_validator_to_reject(self):
        self.assertEqual(m.sanitize_tags('#a,#b'), '#a,#b')

    def test_validator_is_unchanged_and_still_strict(self):
        with self.assertRaises(m.ValidationError):
            m.render(dict(GOOD, tags=['#Travel', '#bad tag', '#Film']))
        text = m.render(dict(GOOD, tags=m.sanitize_tags(['Travel', 'solo-travel', '#Film 🎬'])))
        self.assertIn('#Travel, #SoloTravel, #Film, #yt:cc=on', text)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'done' / 'P'
        (self.project / 'subs').mkdir(parents=True)
        (self.project / 'subs/final.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\nWe walk through town.\n')
        (self.project / 'youtube_video_id.txt').write_text('abcdefghijk')
        notes = self.base / 'notes'; notes.mkdir()
        for name, value in (('BASE', self.base), ('EDITORIAL_DIR', notes)):
            p = patch.object(m, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(m.time, 'sleep'); p.start(); self.addCleanup(p.stop)

    def run_process(self, outputs):
        with patch.object(m, 'request_metadata', side_effect=outputs) as api, patch('builtins.print'):
            result = m.process(self.project, self.project, run=True, key='k')
        return result, api

    def test_messy_model_tags_publish_without_repair_call(self):
        messy = dict(GOOD, tags=['travel', 'solo-travel', '#night market 🌃', '#Film'])
        result, api = self.run_process([messy])
        self.assertEqual(api.call_count, 1)                      # no LLM repair needed
        self.assertFalse((self.project / '.metadata_needs_review').exists())
        meta = (self.project / 'meta.txt').read_text()
        self.assertIn('#travel, #SoloTravel, #NightMarket, #Film, #yt:cc=on', meta)

    def test_unsalvageable_tags_still_use_single_repair_then_review(self):
        bad = dict(GOOD, tags=['🌍', '🗼', '#'])
        result, api = self.run_process([bad, bad])
        self.assertEqual(api.call_count, 2)
        self.assertTrue(result.startswith('NEEDS REVIEW'))
        self.assertFalse((self.project / 'meta.txt').exists())


class UploadRequestTests(unittest.TestCase):
    def test_rendered_meta_parses_to_strict_tags_and_unlisted(self):
        text = m.render(dict(GOOD, tags=m.sanitize_tags(['a b', '#c-d', 'Eé'])))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'meta.txt'; path.write_text(text)
            meta = up.parse_meta(path)
        self.assertEqual(meta['privacy'], 'unlisted')
        tags = [t.strip() for t in meta['tags'].split(',')]
        self.assertEqual(tags, ['#AB', '#CD', '#Ee', '#yt:cc=on'])

    def test_make_upload_request_only_builds_insert_and_never_executes(self):
        service = MagicMock()
        body = {'snippet': {'title': 't'}, 'status': {'privacyStatus': 'unlisted'}}
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / 'v.mp4'; video.write_bytes(b'\0' * 1024)
            req = up.make_upload_request(service, video, body)
        kwargs = service.videos.return_value.insert.call_args.kwargs
        self.assertEqual(kwargs['part'], 'snippet,status')
        self.assertEqual(kwargs['body'], body)
        self.assertEqual(kwargs['body']['status']['privacyStatus'], 'unlisted')
        self.assertIs(req, service.videos.return_value.insert.return_value)
        req.execute.assert_not_called(); req.next_chunk.assert_not_called()
        service.videos.return_value.update.assert_not_called()


class OAuthRefreshErrorTests(unittest.TestCase):
    def test_classification_of_refresh_failures(self):
        revoked = RefreshError('invalid_grant: Token has been expired or revoked.')
        self.assertTrue(up.is_auth_required_error(revoked))
        self.assertFalse(up.is_temporary_network_error(revoked))      # must not be retried silently
        flaky = RefreshError('Unable to acquire impersonated credentials')
        self.assertFalse(up.is_auth_required_error(flaky))
        self.assertTrue(up.is_temporary_network_error(flaky))

    def test_check_service_requires_reauth_when_refresh_leaves_creds_invalid(self):
        creds = MagicMock(expired=True, refresh_token='x', valid=False)
        creds.has_scopes.return_value = True
        creds.refresh.side_effect = lambda req: None
        with tempfile.TemporaryDirectory() as d, patch.object(up, 'CHECK_TOKEN', Path(d) / 'tok'), \
                patch.object(up.pickle, 'load', return_value=creds), patch.object(up, 'build') as build:
            (Path(d) / 'tok').write_bytes(b'x')
            with self.assertRaises(up.AuthRequired):
                up.get_check_service()
        build.assert_not_called()

    def test_check_service_refresh_error_propagates_without_building_client(self):
        creds = MagicMock(expired=True, refresh_token='x')
        creds.has_scopes.return_value = True
        creds.refresh.side_effect = RefreshError('invalid_grant')
        with tempfile.TemporaryDirectory() as d, patch.object(up, 'CHECK_TOKEN', Path(d) / 'tok'), \
                patch.object(up.pickle, 'load', return_value=creds), patch.object(up, 'build') as build:
            (Path(d) / 'tok').write_bytes(b'x')
            with self.assertRaises(RefreshError):
                up.get_check_service()
        build.assert_not_called()

    def test_verify_maps_refresh_error_to_pending_not_reupload(self):
        with patch.object(up, 'get_check_service', side_effect=RefreshError('invalid_grant')), patch.object(up, 'log'):
            with self.assertRaises(up.VerificationPending) as ctx:
                up.verify_video_exists('abcdefghijk')
        self.assertIn('AUTH_REFRESH_ERROR', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
