# youtube-automation

Production-tested YouTube publishing and automation pipeline (private snapshot).

## Layout
- `yt_autoupload.py`, `yt_autowatch.py`, `yt_check_auth.py`, `prepare.py` — queue watcher / upload entry points
- `tools/` — metadata, subtitles, thumbnails, publishing, upload throttling
- `tests/` — unit tests
- `PUBLISHING_WORKFLOW.md` — publishing workflow notes

## Status
This is a snapshot of a live installation at `~/YT_QUEUE`. Secrets, tokens, runtime state
and media are intentionally excluded (see `.gitignore`, `.env.example`).

## Known portability issues
- Code assumes `~/YT_QUEUE` as base directory.
- Some hardcoded absolute paths (pyenv Python, demucs/whisper binaries, an Obsidian vault directory).
- Requires locally created Google OAuth credentials; see `.env.example`.
