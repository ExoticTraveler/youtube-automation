#!/usr/bin/env python3
"""Reconcile a false-positive Uploader GPT review with an independent API re-review.

Trust boundary: owner one-shot command -> trusted-main exact-SHA CI and full
head source -> one bounded GPT review -> immutable V2 receipt. No Claude API,
repair, PR merge, deployment, media, or Mac actions.
"""
import json
import os
import sys
from pathlib import Path

import youtube_autodev_controller as ctl

PR = 1
COMMAND = "YT-AUTODEV-REVIEW-V2 sha=3d0b82134c2298a78325e4b574231b0bccfd068a"


def owner_event(ev):
    c = ev.get("comment") or {}
    issue = ev.get("issue") or {}
    return (os.environ.get("GITHUB_REPOSITORY") == ctl.REPO
            and os.environ.get("GITHUB_EVENT_NAME") == "issue_comment"
            and ev.get("action") == "created"
            and issue.get("number") == PR and bool(issue.get("pull_request"))
            and c.get("user", {}).get("login") == "ExoticTraveler"
            and c.get("author_association") == "OWNER"
            and c.get("body") == COMMAND)


def old_review_exists(receipts, sha):
    review = ctl.marker("REVIEW", PR, sha)
    claim = ctl.marker("FIRE-CLAIM", PR, sha)
    fired = ctl.marker("FIRED", PR, sha)
    old = [b for b in receipts if b.startswith(review)]
    claims = [b for b in receipts if b.startswith(claim)]
    if (len(old) != 1 or not old[0].startswith(review + "\nDecision: FAIL_ACTIONABLE\n")
            or len(claims) != 1
            or any(b.startswith(fired) for b in receipts)):
        raise RuntimeError("historical reviewer / fire ledger not safely reconcilable")
    if not ("re" in old[0] and "unicodedata" in old[0]):
        raise RuntimeError("historical false-positive finding does not match scope")


def check_no_stale_changes(t, sha):
    # We never accept branch movement or edits from the previous uncertain fire.
    if ctl.live(t)["head"]["sha"] != sha or sha != t["initial_sha"]:
        raise RuntimeError("Uploader PR head moved; cannot re-review pinned SHA")
    files = ctl.gh("/pulls/1/files?per_page=100&page=1")
    ctl.safe_changed_files(files, set(t["allowed_files"]))
    if len(files) >= 100:
        raise RuntimeError("unbounded uploader PR file list")


def review_once():
    cfg = ctl.cfg()
    vad, up = cfg["targets"]
    sha = up["initial_sha"]
    # Original VAD exact-SHA PASS must still hold; this is not a new VAD review.
    if ctl.live(vad)["head"]["sha"] != vad["initial_sha"]:
        raise RuntimeError("VAD PR head changed; pause parent queue")
    vad_receipts = ctl.receipts(5)
    if not any(b.startswith(ctl.marker("REVIEW", 5, vad["initial_sha"]) + "\nDecision: PASS\n")
               for b in vad_receipts):
        raise RuntimeError("VAD original bot PASS receipt missing")
    check_no_stale_changes(up, sha)
    existing = ctl.receipts(PR)
    old_review_exists(existing, sha)
    claim = ctl.marker("REVIEW-V2-CLAIM", PR, sha)
    verdict_marker = ctl.marker("REVIEW-V2", PR, sha)
    if any(b.startswith(claim) or b.startswith(verdict_marker) for b in existing):
        print("SKIP_ALREADY_CLAIMED: V2 review is non-idempotent")
        return
    run = ctl.check_ci(up, sha)
    if run is None:
        raise RuntimeError("Uploader exact-SHA CI missing or in progress")
    # Full-head source is required; failure before the claim is safe to fix.
    patch = ctl.pr_patch(up)
    source = ctl.full_head_source(up, sha)
    if "import re\n" not in source or "import unicodedata\n" not in source:
        raise RuntimeError("known import evidence missing; stop before requesting GPT")
    if ctl.live(up)["head"]["sha"] != sha:
        raise RuntimeError("Uploader head moved before review claim")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY missing")
    ctl.note(PR, claim + "\nOne-shot re-review of historical false-positive after source+CI verification.")
    try:
        # gpt_review independently reads exact-SHA full source, together
        # with the patch and verified CI, and returns strict structured JSON.
        answer = ctl.gpt_review(up, sha, run, patch)
    except (RuntimeError, ValueError, KeyError):
        raise RuntimeError("V2 GPT API review outcome uncertain after claim; NO AUTO RETRY") from None
    if ctl.live(up)["head"]["sha"] != sha:
        raise RuntimeError("PR head changed while V2 GPT reviewed; no verdict posted")
    decision = answer["decision"]
    body = (verdict_marker + "\nDecision: " + decision
            + "\nExact SHA: " + sha
            + "\nCI run: " + str(run)
            + "\nFindings: " + answer["findings"][:2500]
            + "\nNext: " + answer["next_action"][:900]
            + "\nV2 review supersedes ONLY the diff-only false-positive for code review. "
              "No Claude retry, media, deployment, Mac E2E or PR merge.")
    ctl.note(PR, body)
    if decision != "PASS":
        print("OWNER_GATE: GPT V2 " + decision + ", old Claude fire still uncertain; no dispatch")
        return
    # Controller's normal future reconciliation now accepts this exact bot
    # code-only PASS without pretending the original fire succeeded.
    if ctl.review_once(vad, cfg) != "PASS" or ctl.review_once(up, cfg) != "PASS":
        raise RuntimeError("parent code-only checks did not both PASS")
    parent = ctl.marker("PARENT-CODE-COMPLETE", 5, vad["initial_sha"])
    if not any(b.startswith(parent) for b in ctl.receipts(5)):
        ctl.note(5, parent
                 + "\nVAD PR #5 and Uploader PR #1 PASS exact-SHA GPT code-only reviews "
                   "(Uploader re-reviewed V2). Mac/editor/integration, merge and production gates pending.")
    print("PARENT_CODE_REVIEW_COMPLETE / CODE_PASS_WAITING_MAC")


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if not owner_event(event):
        print("SKIP: unrelated event")
        return
    review_once()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        sys.exit(2)
