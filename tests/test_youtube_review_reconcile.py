"""Offline tests for historical Uploader exact-SHA review reconciliation.

Never invokes network, Claude, real GPT API, local Mac or media.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import youtube_autodev_controller as ctl

spec = importlib.util.spec_from_file_location("uploader_reconcile", ROOT / "tools/youtube_review_reconcile.py")
recon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recon)


class ReconcileSafety(unittest.TestCase):
    def event(self):
        return {"action": "created", "issue": {"number": 1, "pull_request": {}},
                "comment": {"user": {"login": "ExoticTraveler"}, "author_association": "OWNER",
                            "body": recon.COMMAND}}

    def test_exact_owner_comment_only(self):
        ev = self.event()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ctl.REPO,
                                    "GITHUB_EVENT_NAME": "issue_comment"}):
            self.assertTrue(recon.owner_event(ev))
            ev["comment"]["body"] += " injected"
            self.assertFalse(recon.owner_event(ev))
            ev["comment"]["body"] = recon.COMMAND
            ev["comment"]["user"]["login"] = "other"
            self.assertFalse(recon.owner_event(ev))

    def test_historical_fail_and_claim_must_match_exact_sha(self):
        sha = ctl.cfg()["targets"][1]["initial_sha"]
        rev = ctl.marker("REVIEW", 1, sha)
        fire = ctl.marker("FIRE-CLAIM", 1, sha)
        rows = [rev + "\nDecision: FAIL_ACTIONABLE\nFindings: missing re and unicodedata imports",
                fire + "\nclaimed"]
        recon.old_review_exists(rows, sha)
        for bad in (rows[:1], [rows[0], rows[1], rows[1]],
                    rows + [ctl.marker("FIRED", 1, sha)],
                    [rev + "\nDecision: PASS\nFindings: re unicodedata", rows[1]]):
            with self.subTest(rows=bad), self.assertRaises(RuntimeError):
                recon.old_review_exists(bad, sha)

    def test_unconfirmed_fire_only_bypassed_by_exact_bot_v2_pass(self):
        t = ctl.cfg()["targets"][1]
        sha = t["initial_sha"]
        old = ctl.marker("FIRE-CLAIM", 1, sha) + "\nclaimed"
        v2_pass = ctl.marker("REVIEW-V2", 1, sha) + "\nDecision: PASS\nCI run: 123"
        allowed = [{"filename": "tools/metadata.py", "status": "modified"}]
        with patch.object(ctl, "gh", return_value=allowed):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                ctl.check_code_scope(t, sha, [old])
        with patch.object(ctl, "gh", return_value=allowed):
            ctl.check_code_scope(t, sha, [old, v2_pass])
        with patch.object(ctl, "gh", return_value=allowed):
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                ctl.check_code_scope(t, "a" * 40, [old, v2_pass])

    def test_reviewer_fail_never_fires_claude(self):
        cfg = ctl.cfg()
        vad, up = cfg["targets"]
        old = [ctl.marker("REVIEW", 1, up["initial_sha"]) +
               "\nDecision: FAIL_ACTIONABLE\nFindings: hypothetical missing re and unicodedata imports",
               ctl.marker("FIRE-CLAIM", 1, up["initial_sha"]) + "\nclaim"]
        vadbot = [ctl.marker("REVIEW", 5, vad["initial_sha"]) + "\nDecision: PASS\nold"]
        def receipt(pr):
            return vadbot if pr == 5 else old
        with patch.object(ctl, "live", side_effect=lambda t: {"head": {"sha": t["initial_sha"]}}), \
             patch.object(ctl, "receipts", side_effect=receipt), \
             patch.object(recon, "check_no_stale_changes"), \
             patch.object(ctl, "check_ci", return_value=123), \
             patch.object(ctl, "pr_patch", return_value="metadata patch"), \
             patch.object(ctl, "full_head_source", return_value="import re\nimport unicodedata\n"), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "mock-secret"}), \
             patch.object(ctl, "note") as note, \
             patch.object(ctl, "gpt_review", return_value={
                "decision": "FAIL_ACTIONABLE", "findings": "actual hypothetical defect",
                "next_action": "reproduce before any code edit"}) as review, \
             patch.object(ctl, "fire") as fire:
            recon.review_once()
            self.assertEqual(review.call_count, 1)
            self.assertEqual(note.call_count, 2)  # claim + V2 verdict, no fire
            self.assertTrue(note.call_args_list[1].args[1].startswith(
                ctl.marker("REVIEW-V2", 1, up["initial_sha"]) + "\nDecision: FAIL_ACTIONABLE"))
            fire.assert_not_called()

    def test_verified_v2_pass_closes_code_only_parent(self):
        cfg = ctl.cfg()
        t5,t1 = cfg["targets"]
        old = [ctl.marker("REVIEW", 1, t1["initial_sha"]) +
               "\nDecision: FAIL_ACTIONABLE\nFindings: missing re and unicodedata imports",
               ctl.marker("FIRE-CLAIM", 1, t1["initial_sha"])]
        vadbot = [ctl.marker("REVIEW", 5, t5["initial_sha"]) + "\nDecision: PASS\nold"]
        receipts_map = {1:old,5:vadbot}
        records=[]
        def note(pr,body):
            records.append((pr,body))
            receipts_map[pr].append(body)
            return 123
        with patch.object(ctl, "live", side_effect=lambda t: {"head":{"sha":t["initial_sha"]}}), \
             patch.object(ctl, "receipts", side_effect=lambda pr:receipts_map[pr]), \
             patch.object(recon, "check_no_stale_changes"), \
             patch.object(ctl, "check_code_scope"), \
             patch.object(ctl, "check_ci", return_value=123), \
             patch.object(ctl, "pr_patch", return_value="real diff"), \
             patch.object(ctl, "full_head_source", return_value="import re\nimport unicodedata\n"), \
             patch.dict(os.environ, {"OPENAI_API_KEY":"mock-secret"}), \
             patch.object(ctl, "note", side_effect=note), \
             patch.object(ctl, "gpt_review", return_value={
                "decision":"PASS","findings":"no reproduced defect","next_action":"Mac review"
             }), \
             patch.object(ctl, "fire") as fire:
            recon.review_once()
            self.assertTrue(any(pr==1 and "REVIEW-V2 pr=1" in b and "Decision: PASS" in b
                                for pr,b in records))
            self.assertTrue(any(pr==5 and "PARENT-CODE-COMPLETE" in b for pr,b in records))
            fire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
