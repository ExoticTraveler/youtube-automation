#!/usr/bin/env python3
"""One-shot authenticated Claude Routine API smoke test; no repository edits.

Standalone, deliberately separate from the AutoDev FAIL->repair ledger.
Only an owner comment on the fixed Uploader PR can authorize a probe.
Never re-fire after a claim, regardless of the API response.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO = "ExoticTraveler/youtube-automation"
PR = 1
SHA = "3d0b82134c2298a78325e4b574231b0bccfd068a"
BRANCH = "claude/hashtag-validation-backup-20261031"
ROUTINE = "trig_01XubzuYGWQaR56yQZ52mmb5"
FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/" + ROUTINE + "/fire"
COMMAND = "YT-AUTODEV-API-PROBE-V2 sha=" + SHA
CLAIM = "<!-- YT-AUTODEV-API-PROBE-V2-CLAIM pr=1 sha=" + SHA + " -->"
FIRED = "<!-- YT-AUTODEV-API-PROBE-V2-FIRED pr=1 sha=" + SHA + " -->"
OLD_CLAIM = "<!-- YT-AUTODEV-API-PROBE-CLAIM pr=1 sha=" + SHA + " -->"
OLD_FIRED = "<!-- YT-AUTODEV-API-PROBE-FIRED pr=1 sha=" + SHA + " -->"
GH_BASE = "https://api.github.com/repos/" + REPO


def api(url, token, payload=None, *, github=False):
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json" if github else "application/json",
        "User-Agent": "youtube-autodev-one-shot-api-probe",
    }
    if github:
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    else:
        headers["anthropic-version"] = "2026-10-21"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    method = "POST" if data is not None else "GET"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # No error body, credentials, or external URL. 4xx does not authorize retry.
        suffix = (url[len(GH_BASE):].split("?", 1)[0]
                  if github and url.startswith(GH_BASE) else "")
        raise RuntimeError(
            ("GitHub API" + (" " + suffix if suffix else "") if github else "Claude Routine API")
            + " HTTP " + str(exc.code)
        ) from None
    except urllib.error.URLError:
        raise RuntimeError("API network failure; outcome uncertain, DO NOT RETRY") from None


def gh(path, payload=None):
    return api(GH_BASE + path, os.environ["GH_TOKEN"], payload, github=True)


def get_comments():
    all_comments = []
    for page in range(1, 6):
        items = gh("/issues/1/comments?per_page=100&page=" + str(page))
        all_comments.extend(items)
        if len(items) < 100:
            return all_comments
    raise RuntimeError("Too many comments; declining to send another routine fire")


def note(body):
    created = gh("/issues/1/comments", {"body": body})
    if not isinstance(created.get("id"), int):
        raise RuntimeError("GitHub bot receipt unavailable")
    verified = gh("/issues/comments/" + str(created["id"]))
    if verified.get("body") != body or verified.get("user", {}).get("login") != "github-actions[bot]":
        raise RuntimeError("GitHub bot receipt failed verification")


def verify_command(event):
    issue = event.get("issue") or {}
    c = event.get("comment") or {}
    if (os.environ.get("GITHUB_EVENT_NAME") != "issue_comment"
        or os.environ.get("GITHUB_REPOSITORY") != REPO
        or event.get("action") != "created"
        or issue.get("number") != PR or not issue.get("pull_request")
        or c.get("user", {}).get("login") != "ExoticTraveler"
        or c.get("author_association") != "OWNER"
        or str(c.get("body", "")).strip() != COMMAND):
        print("SKIP: no authorized one-shot probe")
        return False
    pr = gh("/pulls/1")
    if (pr.get("state") != "open" or pr.get("head", {}).get("sha") != SHA
        or pr.get("head", {}).get("ref") != BRANCH
        or pr.get("head", {}).get("repo", {}).get("full_name") != REPO
        or pr.get("base", {}).get("ref") != "main"):
        raise RuntimeError("PR head, branch, base or repo drift; no API probe")
    return True


def fire():
    url = os.environ.get("CLAUDE_ROUTINE_FIRE_URL", "").strip()
    token = os.environ.get("CLAUDE_ROUTINE_FIRE_TOKEN", "").strip()
    # Regenerating an API trigger can change its trig_* ID, while the saved
    # routine and its repository remain the same. Validate the fixed Anthropic
    # host/path only: the per-trigger bearer token binds to that trigger.
    # The exact URL and token are owner-entered YouTube repository secrets.
    if not re.fullmatch(
        r"https://api\.anthropic\.com/v1/claude_code/routines/trig_[A-Za-z0-9_-]+/fire",
        url
    ):
        raise RuntimeError("Routine Fire URL does not match the allowed Anthropic endpoint; no request sent")
    # The Claude API documents the per-Routine token prefix but does not
    # guarantee a specific character class for the remainder of the token.
    if not token.startswith("sk-ant-oat01-") or len(token) <= len("sk-ant-oat01-"):
        raise RuntimeError("Claude Routine token is missing or does not have its required prefix; no request sent")
    # Critically, the claim is durable before the POST; do not retry uncertain runs.
    note(CLAIM + "\nOne-shot AUTH probe claim; non-idempotent Routine POST may occur once.")
    text = (
        "CONNECTIVITY PROBE ONLY for GitHub owner-authorized YouTube AutoDev. "
        "This is NOT a GPT FAIL_ACTIONABLE review, code repair, commit authorization "
        "or a request to run media, tools, tests, or external APIs. "
        "Work on no files; do not alter PRs, branches, video, OAuth, secrets or GitHub. "
        "Reply briefly 'YT_AUTODEV_API_CONNECTION_OK_V2' and stop. "
        "This diagnostic session does NOT override existing PR #1 or #5 decisions."
    )
    try:
        result = api(url, token, {"text": text})
    except RuntimeError as exc:
        # No further POST allowed for the same claimed SHA, even if the error
        # happens to be HTTP 4xx. Caller can fix/reconcile separately.
        raise RuntimeError(str(exc) + "; claim exists, DO NOT RETRY") from None
    if result.get("type") != "routine_fire":
        raise RuntimeError("Unknown Routine response after POST; DO NOT RETRY")
    link = result.get("claude_code_session_url", "")
    parsed = urlsplit(link) if isinstance(link, str) else None
    if (not parsed or parsed.scheme != "https" or parsed.netloc != "claude.ai"
        or not re.fullmatch(r"/code/session_[A-Za-z0-9_-]+", parsed.path)
        or parsed.query or parsed.fragment):
        raise RuntimeError("Routine accepted but returned an invalid session URL; DO NOT RETRY")
    note(FIRED + "\nClaude Routine API accepted connection smoke session.\n" + link)
    print("PASS: CLAUDE_ROUTINE_API_ACCEPTED; session link recorded in verified GitHub receipt")


def main():
    ev = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if not verify_command(ev):
        return
    old = get_comments()
    if any(c.get("user", {}).get("login") == "github-actions[bot]"
           and str(c.get("body", "")).startswith(CLAIM) for c in old):
        print("SKIP: probe already claimed; NEVER retry")
        return
    # Require an old durable claim with no successful FIRED receipt.
    # This does not retry an old invocation; V2 is a separate owner-authorized
    # connectivity attempt after the original was rejected with HTTP 401.
    prior_bot = [str(c.get("body", "")) for c in old
                 if c.get("user", {}).get("login") == "github-actions[bot]"]
    if not any(body.startswith(OLD_CLAIM) for body in prior_bot):
        raise RuntimeError("missing original probe claim; refusing V2 API request")
    if any(body.startswith(OLD_FIRED) for body in prior_bot):
        raise RuntimeError("original probe already fired successfully; no V2 API request")
    fire()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, ValueError, OSError) as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        sys.exit(2)
