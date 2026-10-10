#!/usr/bin/env python3
"""MANUAL, OPT-IN runner for the EXISTING thumbnail production (tools/thumbnail.py --run).

*** Nothing in the YouTube pipeline calls this module. Automatic thumbnail generation was removed
*** from yt_autowatch.py on 2026-10-03 (paid image API; cost/quality does not yet justify running
*** it for every video). Run it only intentionally:
***   python3 ~/YT_QUEUE/tools/thumbnail_auto.py --project done/NAME --run      (one project, locked, billing-safe)
***   python3 ~/YT_QUEUE/tools/thumbnail.py done/NAME --dry-run | --run         (the bare existing CLI)
*** `--scan --run` (batch mode) additionally needs the opt-in file `.thumbnail_auto_since`, which is
*** currently DISABLED (renamed `.thumbnail_auto_since.DISABLED_20261003`), so batch mode starts nothing.

It was originally written as a pipeline trigger. It contains no thumbnail logic: it decides whether a
project is ready, runs the existing CLI once, records state and stops for human selection.

This is only a trigger. It contains no thumbnail logic: it decides whether a verified
done/ project is ready, runs the existing CLI once, records state and stops for human
selection. It never chooses a thumbnail, never calls YouTube, never touches the video,
final.srt, meta.txt or youtube_video_id.txt, and never deletes existing thumbnail output.

Default is read-only (decisions are printed). --run opts into starting a job.

Eligible when ALL hold:
  - project is a real directory directly under done/ with a saved 11-character YouTube video ID
    whose file is newer than the queue's `.thumbnail_auto_since` cut-off (prevents the historical
    back-catalogue from triggering paid generation; the file holds an ISO timestamp or epoch)
  - subs/final.srt is valid (final_subtitle_autogen.final_status == DONE) and settled (>=120 s old)
  - meta.txt is a regular, parseable file with a valid title/description and settled (>=120 s old)
  - no completed production package (thumbnail/production/manifest.json), no human thumbnail file
    (uploader convention thumbnail.* / Thumbnail…), no `.thumbnail_needs_review`
  - enough free disk space and no job already running (global exclusive lock)

State (per project, atomic JSON): `.thumbnail_state.json` = RUNNING | FAILED | COMPLETE with attempts,
errors and retry_after. Job output: logs/thumbnail_auto/<project>_<time>.log (private).
A failed job is retried at most MAX_ATTEMPTS times, one hour apart, then `.thumbnail_needs_review`
is written and automatic (paid) retries stop.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = Path.home() / 'YT_QUEUE'
PYTHON = str(Path.home() / '.pyenv/versions/3.10.16/bin/python3')
SINCE_NAME = '.thumbnail_auto_since'
SETTLE_S = 120
MAX_ATTEMPTS = 2
BACKOFF_S = 3600
MIN_FREE_BYTES = 3 * 1024 ** 3
JOB_TIMEOUT_S = 4 * 3600
THUMB_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
# Account-level billing failures cannot succeed on retry and each retry repeats paid planning/vision calls.
BILLING = re.compile(r'credit_balance_exhausted|insufficient_quota|billing_hard_limit|exceeded your current quota', re.I)
# launchd starts the watcher with a minimal PATH; ffmpeg/ffprobe live in Homebrew (as in sub_autogen.py).
TOOL_PATH = '/opt/homebrew/bin:/usr/local/bin:' + os.environ.get('PATH', '/usr/bin:/bin')


def child_env():
    env = dict(os.environ)
    env['PATH'] = TOOL_PATH
    return env


def preflight():
    """Missing runtime pieces are an environment problem, not a failed attempt: report and wait."""
    missing = [name for name in ('ffmpeg', 'ffprobe') if not shutil.which(name, path=TOOL_PATH)]
    venv = BASE / 'tools/.thumbnail_venv/bin/python'
    if not venv.exists():
        missing.append('tools/.thumbnail_venv')
    key = BASE / 'secrets/openai_api_key'
    if not os.environ.get('OPENAI_API_KEY') and not key.exists():
        missing.append('OpenAI API key')
    return missing


def log(message):
    print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}', flush=True)


def read_json(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    fd, name = tempfile.mkstemp(prefix='.thumbnail_state.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2)
            handle.write('\n')
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def cutoff():
    """Epoch seconds from .thumbnail_auto_since, or None (feature off)."""
    try:
        text = (BASE / SINCE_NAME).read_text().strip()
    except OSError:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def human_thumbnail(project):
    for p in sorted(project.iterdir()):
        if p.is_file() and not p.name.startswith('.') and p.suffix.lower() in THUMB_EXTS \
                and p.stem.lower().startswith('thumbnail'):
            return p
    return None


def valid_final(project):
    from final_subtitle_autogen import final_status
    try:
        return final_status(project / 'subs') == 'DONE'
    except Exception:
        return False


def valid_meta(path):
    """Regular file with parseable title/description (same essentials publishing.py checks)."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        import yt_autoupload as uploader
        meta = uploader.parse_meta(path)
    except Exception:
        return False
    title, description = meta.get('title', ''), meta.get('description', '')
    return (bool(title.strip()) and len(title) <= 100 and '\n' not in title and '<' not in title and '>' not in title
            and bool(description.strip()) and len(description.encode('utf-8')) <= 5000
            and '<' not in description and '>' not in description)


def evaluate(project, now=None, manual=False):
    """Return (status, detail). status: READY | WAIT | SKIP | REVIEW.

    manual=True (explicit `--project`) skips the batch opt-in cut-off; every other safety check still applies."""
    now = time.time() if now is None else now
    if project.is_symlink() or not project.is_dir() or project.name.startswith('.'):
        return 'SKIP', 'not a project directory'
    if project.parent != BASE / 'done':
        return 'SKIP', 'not in done/'
    since = 0.0 if manual else cutoff()
    if since is None:
        return 'SKIP', f'batch thumbnail mode disabled ({SINCE_NAME} missing/invalid); use --project NAME --run'
    id_file = project / 'youtube_video_id.txt'
    try:
        video_id = id_file.read_text().strip()
        id_mtime = id_file.stat().st_mtime
    except OSError:
        return 'SKIP', 'no saved YouTube video ID'
    if not VIDEO_ID.fullmatch(video_id):
        return 'SKIP', 'invalid saved YouTube video ID'
    if id_mtime < since:
        return 'SKIP', 'upload predates auto-thumbnail cut-off (legacy project)'
    if (project / 'thumbnail/production/manifest.json').is_file():
        return 'SKIP', 'production package already complete'
    if human_thumbnail(project):
        return 'SKIP', 'thumbnail file already present'
    if (project / '.thumbnail_needs_review').exists():
        return 'REVIEW', 'needs review; automatic retries stopped'
    final = project / 'subs/final.srt'
    if not final.is_file() or not valid_final(project):
        return 'WAIT', 'valid final.srt missing'
    if not valid_meta(project / 'meta.txt'):
        return 'WAIT', 'valid meta.txt missing'
    for path in (final, project / 'meta.txt'):
        if now - path.stat().st_mtime < SETTLE_S:
            return 'WAIT', f'{path.name} not settled yet'
    state = read_json(project / '.thumbnail_state.json')
    if state.get('status') == 'FAILED' and BILLING.search(str(state.get('log_tail', ''))):
        return 'REVIEW', 'OpenAI billing/credit failure; add credits, then run thumbnail_auto.py --reset PROJECT'
    if state.get('status') == 'COMPLETE':
        return 'SKIP', 'state says complete'
    if state.get('status') == 'FAILED' and now < float(state.get('retry_after', 0) or 0):
        return 'WAIT', f"retry after {datetime.fromtimestamp(state['retry_after']):%H:%M:%S} (attempt {state.get('attempts', 0)})"
    return 'READY', 'eligible'


def tail(path, lines=25):
    try:
        return ''.join(path.read_text(errors='replace').splitlines(True)[-lines:])
    except OSError:
        return ''


def default_runner(command, log_path, timeout):
    with open(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                              timeout=timeout, env=child_env()).returncode


def run_job(project, runner=default_runner, now=None, manual=False):
    """Run the existing production CLI for one eligible project; idempotent and exclusive."""
    lock_path = BASE / '.thumbnail_auto.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'SKIP another thumbnail job is running'
        status, detail = evaluate(project, now, manual)  # re-check under the lock
        if status != 'READY':
            return f'{status} {detail}'
        if shutil.disk_usage(project).free < MIN_FREE_BYTES:
            return 'WAIT insufficient free disk space (< 3 GiB)'
        missing = preflight()
        if missing:
            return 'WAIT runtime missing: ' + ', '.join(missing)
        state_path = project / '.thumbnail_state.json'
        previous = read_json(state_path)
        attempts = int(previous.get('attempts', 0) or 0) + 1
        started = time.time() if now is None else now
        write_json(state_path, {'status': 'RUNNING', 'attempts': attempts, 'started_at': started, 'pid': os.getpid()})
        log_dir = BASE / 'logs/thumbnail_auto'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f'{project.name}_{datetime.now():%Y%m%dT%H%M%S}_attempt{attempts}.log'
        command = [PYTHON, '-B', str(BASE / 'tools/thumbnail.py'), str(project), '--run']
        log(f'{project.name}: STARTING thumbnail production attempt {attempts}: {" ".join(command)} (log {log_path})')
        error = None
        try:
            code = runner(command, log_path, JOB_TIMEOUT_S)
        except Exception as exc:  # includes timeouts: never leave RUNNING behind
            code, error = None, f'{type(exc).__name__}: {exc}'
        manifest = project / 'thumbnail/production/manifest.json'
        if code == 0 and manifest.is_file():
            write_json(state_path, {'status': 'COMPLETE', 'attempts': attempts, 'started_at': started,
                                    'finished_at': time.time(), 'output': str(manifest.parent), 'log': str(log_path)})
            return f'COMPLETE {manifest.parent}'
        error = error or (f'exit code {code}' if code else 'exit 0 but no production manifest')
        record = {'status': 'FAILED', 'attempts': attempts, 'started_at': started, 'finished_at': time.time(),
                  'error': error, 'log': str(log_path), 'log_tail': tail(log_path),
                  'retry_after': time.time() + BACKOFF_S}
        write_json(state_path, record)
        if BILLING.search(record['log_tail']):
            (project / '.thumbnail_needs_review').write_text(
                f'{datetime.now():%Y-%m-%dT%H:%M:%S} OpenAI billing/credit failure (not retried automatically).\n'
                f'Add credits, then: thumbnail_auto.py --reset {project.name}\nlog: {log_path}\n')
            return f'FAILED {error}; OpenAI billing/credit failure, automatic retries stopped (.thumbnail_needs_review)'
        if attempts >= MAX_ATTEMPTS:
            (project / '.thumbnail_needs_review').write_text(
                f'{datetime.now():%Y-%m-%dT%H:%M:%S} thumbnail production failed {attempts}x: {error}\nlog: {log_path}\n')
            return f'FAILED {error}; needs review after {attempts} attempts (.thumbnail_needs_review)'
        return f'FAILED {error}; retry after one hour (attempt {attempts}/{MAX_ATTEMPTS})'


def reset(project):
    """Archive (never delete) state + review marker so the project is eligible again. Human action."""
    archive = BASE / 'logs/thumbnail_auto/archive'
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    moved = []
    for name in ('.thumbnail_state.json', '.thumbnail_needs_review'):
        path = project / name
        if path.exists():
            target = archive / f'{project.name}_{stamp}{name}'
            shutil.move(str(path), str(target))
            moved.append(str(target))
    return moved


def resolve(value):
    """Absolute path, `done/NAME` (relative to the queue) or bare NAME (looked up in done/)."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return BASE / path if len(path.parts) > 1 else BASE / 'done' / path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--reset', type=Path, metavar='PROJECT', help='Archive failure state/review marker so the project can run again')
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--run', action='store_true', help='Start production for the first eligible project')
    args = parser.parse_args()
    if args.reset:
        project = args.reset.expanduser()
        project = project if project.is_absolute() else BASE / 'done' / project
        print(f'{project.name}: archived {reset(project) or "nothing"}')
        return 0
    if bool(args.project) == args.scan:
        parser.error('Choose --project or --scan')
    done = BASE / 'done'
    projects = [resolve(args.project)] if args.project else \
        [p for p in sorted(done.iterdir()) if p.is_dir()] if done.is_dir() else []
    started = 0
    for project in projects:
        status, detail = evaluate(project, manual=bool(args.project))
        if not args.run:
            print(f'{project.name}: {status} {detail}', flush=True)
            continue
        if status != 'READY':
            continue  # quiet in the 1-minute watcher loop; use --scan (without --run) to see reasons
        log(f'{project.name}: {run_job(project, manual=bool(args.project))}')
        started += 1
        break  # at most one job per scan
    return 0


if __name__ == '__main__':
    sys.exit(main())
