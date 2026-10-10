"""Local stable-file state and coordination for project directory moves."""
import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
BASE = Path.home() / "YT_QUEUE"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}

def find_video(folder):
    return next((p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in VIDEO_EXTS), None)

@contextmanager
def paths_lock(exclusive=False):
    with (BASE / ".project_paths.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield

def stable(folder, video, log=lambda msg: None, now=None):
    now = time.time() if now is None else now
    with (folder / ".upload_candidate.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = folder / ".upload_candidate.json"
        try:
            old = json.loads(path.read_text())
            if not isinstance(old, dict): old = {}
        except (OSError, ValueError):
            old = {}
        st = video.stat()
        sig = {"filename": video.name, "size": st.st_size, "mtime": st.st_mtime, "mtime_ns": st.st_mtime_ns}
        same = all(old.get(k) == v for k, v in sig.items())
        first = old.get("first_seen", now) if same else now
        if not isinstance(first, (int, float)): first = now
        # Separate observations by a minute, even when two workers scan together.
        ready = same and st.st_size > 0 and now - first >= 60 and now - st.st_mtime >= 120
        state = dict(sig, first_seen=first, last_seen=now, stable=ready)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(path)
        if not same:
            log(f"FOUND VIDEO {folder.name}/{video.name}")
            log(f"WAITING FOR FILE TO STABILIZE {folder.name}")
        elif ready and not old.get("stable"):
            log(f"FILE STABLE {folder.name}/{video.name}")
        return ready
