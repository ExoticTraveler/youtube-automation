"""Offline, synthetic trust-boundary checks for one-shot Routine API probe."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "youtube_routine_probe.py"
spec = importlib.util.spec_from_file_location("yt_probe", SOURCE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeSafety(unittest.TestCase):
    def event(self):
        return {
            "action": "created",
            "issue": {"number": 1, "pull_request": {"url": "some-url"}},
            "comment": {"user": {"login": "ExoticTraveler"},
                        "author_association": "OWNER",
                        "body": probe.COMMAND},
        }

    def test_wrong_commands_refused_without_gh_calls(self):
        ev = self.event()
        ev["comment"]["body"] = probe.COMMAND + " injected"
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "issue_comment",
                                    "GITHUB_REPOSITORY": probe.REPO}), \
             patch.object(probe, "gh") as github:
            self.assertFalse(probe.verify_command(ev))
            github.assert_not_called()

    def test_strict_owner_and_live_sha_check(self):
        ev = self.event()
        good = {"state": "open",
                "head": {"sha": probe.SHA, "ref": probe.BRANCH,
                         "repo": {"full_name": probe.REPO}},
                "base": {"ref": "main"}}
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "issue_comment",
                                    "GITHUB_REPOSITORY": probe.REPO}), \
             patch.object(probe, "gh", return_value=good):
            self.assertTrue(probe.verify_command(ev))
            ev["comment"]["user"]["login"] = "untrusted"
            self.assertFalse(probe.verify_command(ev))
            ev["comment"]["user"]["login"] = "ExoticTraveler"
            good["head"]["sha"] = "0" * 40
            with self.assertRaisesRegex(RuntimeError, "drift"):
                probe.verify_command(ev)

    def test_missing_or_mismatched_secrets_never_send(self):
        with patch.dict(os.environ, {"CLAUDE_ROUTINE_FIRE_URL": "https://wrong.example/fire",
                                    "CLAUDE_ROUTINE_FIRE_TOKEN": "sk-ant-oat01-fake"}), \
             patch.object(probe, "note") as note, patch.object(probe, "api") as api:
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                probe.fire()
            note.assert_not_called()
            api.assert_not_called()

    def test_token_prefix_rejected_without_leaking_value(self):
        with patch.dict(os.environ, {"CLAUDE_ROUTINE_FIRE_URL": probe.FIRE_URL,
                                    "CLAUDE_ROUTINE_FIRE_TOKEN": "invalid_secret_token"}), \
             patch.object(probe, "note") as note, patch.object(probe, "api") as api:
            with self.assertRaisesRegex(RuntimeError, "required prefix") as err:
                probe.fire()
            self.assertNotIn("invalid_secret_token", str(err.exception))
            note.assert_not_called()
            api.assert_not_called()

    def test_claim_is_durable_before_single_post(self):
        token = "sk-ant-oat01-mockonly"
        with patch.dict(os.environ, {"CLAUDE_ROUTINE_FIRE_URL": probe.FIRE_URL,
                                    "CLAUDE_ROUTINE_FIRE_TOKEN": token}), \
             patch.object(probe, "note") as note, \
             patch.object(probe, "api", return_value={
                 "type": "routine_fire",
                 "claude_code_session_url": "https://claude.ai/code/session_test123"
             }) as api:
            probe.fire()
            self.assertEqual(note.call_count, 2)
            self.assertTrue(note.call_args_list[0].args[0].startswith(probe.CLAIM))
            self.assertTrue(note.call_args_list[1].args[0].startswith(probe.FIRED))
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[2].keys(), {"text"})
            self.assertNotIn(token, note.call_args_list[1].args[0])

    def test_failed_post_preserves_claim_and_no_retry(self):
        with patch.dict(os.environ, {"CLAUDE_ROUTINE_FIRE_URL": probe.FIRE_URL,
                                    "CLAUDE_ROUTINE_FIRE_TOKEN": "sk-ant-oat01-mockonly"}), \
             patch.object(probe, "note") as note, \
             patch.object(probe, "api", side_effect=RuntimeError("Claude Routine API HTTP 401")) as api:
            with self.assertRaisesRegex(RuntimeError, "DO NOT RETRY"):
                probe.fire()
            self.assertEqual(note.call_count, 1)
            self.assertEqual(api.call_count, 1)

    def test_existing_claim_never_re_fires(self):
        event = self.event()
        with tempfile.TemporaryDirectory() as temp:
            fp = Path(temp) / "event.json"
            fp.write_text(json.dumps(event))
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(fp)}), \
                 patch.object(probe, "verify_command", return_value=True), \
                 patch.object(probe, "get_comments", return_value=[{
                    "user": {"login": "github-actions[bot]"},
                    "body": probe.CLAIM
                 }]), \
                 patch.object(probe, "fire") as fire:
                probe.main()
                fire.assert_not_called()

    def test_external_http_error_hides_body_and_token(self):
        err = urllib.error.HTTPError(probe.FIRE_URL, 401, "error", {},
                                     io.BytesIO(b"very-sensitive-body"))
        with patch.object(probe.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "Claude Routine API HTTP 401") as caught:
                probe.api(probe.FIRE_URL, "sk-ant-oat01-TOPSECRET", {"text": "probe"})
        self.assertNotIn("TOPSECRET", str(caught.exception))
        self.assertNotIn("very-sensitive", str(caught.exception))
        self.assertNotIn(probe.FIRE_URL, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
