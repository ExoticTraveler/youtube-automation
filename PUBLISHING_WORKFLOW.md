# Post-upload publishing package

`done/<project>/` continues to mean the video upload was verified. Projects stay in place so subtitle workers and saved paths keep working.

Automatic metadata creation now writes `done/<project>/meta.txt` only after a nonempty saved `youtube_video_id.txt` and validated, stable `subs/final.srt` exist. A prepared/failed project waits even if its subtitles finish early. Every existing `meta.txt`, including a dangling symlink, blocks generation. Publication remains atomic and exclusive. The explicit `--output-dir` diagnostic option is retained, but normal scans and subtitle triggers no longer create `meta_review_*` folders. Existing review folders are not moved or deleted; projects that already have real metadata are skipped.

## Readiness

The watcher now also scans `done/`, including when no initial uploads are pending. A project becomes ready when:

- It has a saved 11-character video ID in `done/`.
- `meta.txt` has a valid primary title and nonempty description; tags, category and privacy values are checked when supplied.
- Its final thumbnail is a regular, decodable JPEG/PNG/WebP file using the uploader's unchanged selection rules: exact `thumbnail.jpg`, `.jpeg`, `.png`, `.webp`, then sorted image filenames beginning with `thumbnail`, ignoring case. Multiple alternatives retain existing precedence, not newest-file selection. No new filename or dimension rule is imposed. The API's 50 MiB upload ceiling is checked.
- The same package is observed at least 60 seconds apart and metadata/thumbnail file ages are at least 120 seconds.

`done/<project>/ALL_READY` is a visible JSON marker containing the video ID, selected thumbnail filename and hashes of both inputs. It means the local package is ready, not that YouTube updates have succeeded. It is removed on a subsequent scan if the files are missing, invalid or changing.

`done/<project>/.publishing_state.json` records `PENDING` or `APPLIED`, the fingerprint, per-step success, errors and retry time. `.publishing_candidate.json` tracks stability. Logs are written to `logs/publishing.log` by the watcher. A changed package waits for stability and is eligible for another update. An unchanged successful package is skipped.

## YouTube updates

The new worker uses only `videos.list`, `videos.update(part="snippet")` and `thumbnails.set` against the saved ID. It has no video insertion or video-byte upload path. Remote visibility is checked again before updating. Only editable snippet fields are sent; existing category/languages and absent optional fields are preserved. `title_alternatives` stays local. Privacy and publication scheduling are intentionally preserved, even if `meta.txt` says `privacy: unlisted`.

The thumbnail is uploaded from a snapshot of the validated bytes. Metadata and thumbnail success are checkpointed separately. Failures retry no sooner than one hour; a thumbnail failure does not repeat an already acknowledged metadata update. A crash after a remote success but before local acknowledgement can repeat that same idempotent update, never a video upload. Changes during the operation stop remaining work and prevent marking the changed package applied.

The worker shares the existing uploader and project-path locks. Existing uploader, subtitle validation and upload/check OAuth logic are unchanged.

## Activation (not performed during implementation)

The existing upload-only OAuth scope does not authorize `videos.update`. Update credentials are separate in `secrets/token_update.pickle` with `youtube.force-ssl`; the upload and readonly tokens are untouched. Scheduled scans never launch OAuth.

1. Authorize interactively, without changing a video:
   `~/.pyenv/versions/3.10.16/bin/python3 -B ~/YT_QUEUE/tools/publishing.py --authorize`
2. Enable automatic YouTube updates by creating `~/YT_QUEUE/.publishing_updates_enabled`. Remove this file to disable future automatic updates. Enabling applies to every eligible project in `done/`, including historical projects. Alternatively, `publishing.py --run --apply` explicitly permits updates for that scan only.

Without activation, regular scans still maintain ALL_READY but report `UPDATE DISABLED`. Neither the token nor the enabling file is created by installation or tests.

Read-only inspection: `publishing.py --dry-run`, `yt_autowatch.py --dry-run`, or `tools/metadata.py --scan --dry-run`. These paths do not call APIs or write project state.

## Validation

40 isolated tests passed: the original 18 metadata tests adapted for direct output, plus 22 completion/update regression tests. Remote services are mocks; real image decoding is exercised with a temporary PNG. Tested no-overwrite, validation and source races, initial-upload gating, saved-ID no-reupload behavior, readiness invalidation, stable-file timing, update field preservation, duplicate suppression, partial failure retry, edits during update, and watcher operation with an empty prepared queue.

A read-only scan of a real queue found locally valid packages, some lacking a selected thumbnail and others lacking a saved video ID. No real project file or YouTube resource was changed by those scans.

References: https://developers.google.com/youtube/v3/docs/videos/update and https://developers.google.com/youtube/v3/docs/thumbnails/set
