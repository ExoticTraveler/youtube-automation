"""Synthetic-only tests for trusted-main YouTube AutoDev controller."""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "youtube_autodev_controller.py"
spec = importlib.util.spec_from_file_location("yt_controller", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Scope(unittest.TestCase):
    def test_config_is_two_explicit_targets(self):
        cfg = mod.cfg()
        self.assertEqual([t["pr"] for t in cfg["targets"]], [5, 1])
        self.assertEqual(cfg["max_fires_per_pr"], 2)
        self.assertEqual(cfg["max_reviews_per_pr"], 4)
        self.assertEqual(cfg["targets"][0]["base"], "codex/vad-batch-discovery-20261103")

    def test_comment_and_sha_syntax(self):
        self.assertTrue(mod.START_RE.fullmatch("YT-AUTODEV-START sha=" + "a" * 40))
        self.assertFalse(mod.START_RE.fullmatch("YT-AUTODEV-START sha=" + "x" * 40))
        self.assertFalse(mod.START_RE.fullmatch("YT-AUTODEV-START sha=" + "a" * 40 + " malicious"))
        self.assertFalse(mod.START_RE.fullmatch("please start sha=" + "a" * 40))
        self.assertTrue(mod.ROUTINE_RE.fullmatch(
            "https://api.anthropic.com/v1/claude_code/routines/trig_Abc_123/fire"))
        self.assertFalse(mod.ROUTINE_RE.fullmatch("https://example.com/fire"))

    def test_allowlist_rejects_renames_and_external_paths(self):
        mod.safe_changed_files([{"filename": "tools/vad_batch.py", "status": "modified"}],
                               {"tools/vad_batch.py"})
        for items in (
            [{"filename": ".github/workflows/other.yml", "status": "modified"}],
            [{"filename": "tools/vad_batch.py", "status": "renamed"}],
            [{"filename": "tools/vad_batch.py", "previous_filename": "secrets.txt",
              "status": "renamed"}],
            [],
        ):
            with self.subTest(items=items), self.assertRaises(RuntimeError):
                mod.safe_changed_files(items, {"tools/vad_batch.py"})

    def test_initial_head_pin_and_single_descendant_only(self):
        t = mod.cfg()["targets"][0]
        initial = t["initial_sha"]
        files = [{"filename": "tools/vad_batch.py", "status": "modified"}]
        with patch.object(mod, "gh", return_value=files):
            mod.check_code_scope(t, initial, [])
            with self.assertRaisesRegex(RuntimeError, "before trusted first dispatch"):
                mod.check_code_scope(t, "a" * 40, [])
        claim = mod.marker("FIRE-CLAIM", 5, initial) + "\nClaim."
        self.assertEqual(mod.marker("FIRE-CLAIM", 5, initial),
                         "<!-- YT-AUTODEV-FIRE-CLAIM pr=5 sha=" + initial + " -->")
        with patch.object(mod, "gh", side_effect=[
                files,
                {"status": "ahead", "behind_by": 0, "ahead_by": 1,
                 "commits": [{"sha": "b" * 40}],
                 "files": [{"filename": "tools/vad_batch.py", "status": "modified"}]}]):
            mod.check_code_scope(t, "b" * 40, [claim])
        with patch.object(mod, "gh", side_effect=[
                files, {"status": "ahead", "behind_by": 0, "ahead_by": 2,
                        "commits": [{"sha": "b" * 40}],
                        "files": files}]):
            with self.assertRaisesRegex(RuntimeError, "branch history"):
                mod.check_code_scope(t, "b" * 40, [claim])

    def test_ci_requires_pinned_workflow_and_all_steps(self):
        t = mod.cfg()["targets"][0]
        sha = t["initial_sha"]
        wf = {"sha": t["ci_workflow_blob_sha"]}
        ci = {"workflow_runs": [{
            "name": mod.CI_NAME, "path": mod.CI_PATH,
            "head_sha": sha, "head_repository": {"full_name": mod.REPO},
            "status": "completed", "conclusion": "success",
            "id": 123, "run_number": 1
        }]}
        jobs = {"jobs": [{"name": "regression", "conclusion": "success",
                           "steps": [{"name": x, "conclusion": "success"} for x in t["ci_steps"]]}]}
        with patch.object(mod, "gh", side_effect=[wf, ci, jobs]):
            self.assertEqual(mod.check_ci(t, sha), 123)
        with patch.object(mod, "gh", side_effect=[{"sha": "tampered"}]):
            with self.assertRaisesRegex(RuntimeError, "trust boundary"):
                mod.check_ci(t, sha)
        bad = {"jobs": [{"name": "regression", "conclusion": "success",
                          "steps": [{"name": t["ci_steps"][0], "conclusion": "success"}]}]}
        with patch.object(mod, "gh", side_effect=[wf, ci, bad]):
            with self.assertRaisesRegex(RuntimeError, "required CI step"):
                mod.check_ci(t, sha)

    def test_no_routine_fire_for_same_claimed_sha(self):
        t = mod.cfg()["targets"][0]
        sha = t["initial_sha"]
        with patch.object(mod, "note") as note, patch.object(mod, "req") as request:
            mod.fire(t, sha, 10, [mod.marker("FIRE-CLAIM", 5, sha)], 2)
            note.assert_not_called()
            request.assert_not_called()

    def test_routine_budget_prevents_unbounded_retries(self):
        t = mod.cfg()["targets"][0]
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            mod.fire(t, "c" * 40, 10,
                     [mod.marker("FIRE-CLAIM", 5, "a" * 40),
                      mod.marker("FIRE-CLAIM", 5, "b" * 40)], 2)


if __name__ == "__main__":
    unittest.main()
