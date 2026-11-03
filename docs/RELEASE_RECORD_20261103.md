# YouTube Automation — pending local release record (2026-11-03)

Status: **GitHub review / offline CI pending**, Mac production acceptance **NOT RUN**.

## Saved change under review
- Claude backup commit `ca16c1579dc131f8ffb9f360879b99ae673e670c` adds deterministic hashtag cleanup in `tools/metadata.py`.
- `tests/test_hashtag_and_upload_request.py` covers malformed tags, metadata rendering, request construction (never executes the request), and OAuth refresh error classification with mocked external calls.
- This repository is a sanitized snapshot of a live `~/YT_QUEUE` installation. The **Mac installation remains authoritative**. Do not replace it wholesale with the GitHub snapshot.

## Safe gates
1. Run isolated GitHub CI with no credentials or uploaded video files.
2. Review regression results and diff; no automatic merge on CI PASS alone.
3. On the Mac, compare only relevant changed code paths against the installed versions; preserve any independent local edits.
4. Back up local originals, run offline tests and read-only queue diagnostics; only then propose targeted integration.
5. YouTube upload, snippet/thumbnail update, OAuth changes, publishing and Mac deployment require separate operator authorization and validation.

## Automation wiring
GitHub Actions `.github/workflows/offline-regression.yml` is on trusted main. This record provides a durable change ledger for review. It does **not** mean Claude Routine/AutoDev for the YouTube repository is connected or running.
