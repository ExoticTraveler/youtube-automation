#!/usr/bin/env python3

import fcntl
from pipeline_state import paths_lock
import os
import sys
import pickle
import subprocess
from pathlib import Path
from datetime import datetime

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


BASE = Path.home() / "YT_QUEUE"

WATCH_DIRS = [
    BASE / "prepared",
    BASE / "done",
]

TOKEN_FILE = BASE / "secrets" / "token_caption.pickle"

HYBRID_SCRIPT = BASE / "tools" / "make_hybrid_srt.py"

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "youtube_caption_fetch.log"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl"
]


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"

    print(line, flush=True)

    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_credentials():
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"Caption token not found: {TOKEN_FILE}"
        )

    with TOKEN_FILE.open("rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

        with TOKEN_FILE.open("wb") as f:
            pickle.dump(creds, f)

    if not creds.valid:
        raise RuntimeError(
            "Caption OAuth credentials are not valid."
        )

    return creds


def get_video_id(folder):
    path = folder / "youtube_video_id.txt"

    if not path.exists():
        return None

    video_id = path.read_text(
        encoding="utf-8"
    ).strip()

    return video_id or None


def collect_targets():
    targets = []

    for root in WATCH_DIRS:
        if not root.exists():
            continue

        for folder in root.iterdir():
            if not folder.is_dir():
                continue

            video_id = get_video_id(folder)

            if not video_id:
                continue

            subs = folder / "subs"
            subs.mkdir(
                parents=True,
                exist_ok=True,
            )

            youtube_srt = subs / "youtube.srt"

            if (
                youtube_srt.exists()
                and youtube_srt.stat().st_size > 0
            ):
                # Retry hybrid when Whisper finishes after captions.
                try:
                    run_hybrid(folder)
                except Exception as exc:
                    log(f"HYBRID FAILED {folder.name}: {exc}", "ERROR")
                continue

            targets.append(
                (folder, video_id)
            )

    targets.sort(
        key=lambda x: x[0].stat().st_mtime
    )

    return targets


def find_best_caption_track(youtube, video_id):
    response = youtube.captions().list(
        part="snippet",
        videoId=video_id,
    ).execute()

    items = response.get("items", [])

    if not items:
        return None

    english_asr = []
    english_other = []
    any_asr = []

    for item in items:
        snippet = item.get("snippet", {})

        language = (
            snippet.get("language", "")
            .lower()
        )

        track_kind = (
            snippet.get("trackKind", "")
            .lower()
        )

        is_asr = track_kind == "asr"

        if language.startswith("en") and is_asr:
            english_asr.append(item)

        elif language.startswith("en"):
            english_other.append(item)

        elif is_asr:
            any_asr.append(item)

    if english_asr:
        return english_asr[0]

    if english_other:
        return english_other[0]

    if any_asr:
        return any_asr[0]

    return None


def download_caption(
    youtube,
    caption_id,
    output_path,
):
    request = youtube.captions().download(
        id=caption_id,
        tfmt="srt",
    )

    data = request.execute()

    if not data:
        raise RuntimeError(
            "Caption download returned empty data."
        )

    if isinstance(data, str):
        data = data.encode("utf-8")

    tmp = output_path.with_suffix(
        ".srt.part"
    )

    tmp.write_bytes(data)

    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)

        raise RuntimeError(
            "Downloaded caption file is empty."
        )

    tmp.replace(output_path)


def final_srt_valid(subs):
    try:
        from final_subtitle_autogen import final_status
        return final_status(subs) == "DONE"
    except Exception:
        return False


def run_hybrid(folder):
    subs = folder / "subs"

    en_srt = subs / "en.srt"
    youtube_srt = subs / "youtube.srt"
    hybrid_srt = subs / "hybrid.srt"

    if hybrid_srt.exists():
        log(
            f"Hybrid already exists: "
            f"{folder.parent.name}/{folder.name}"
        )
        return

    if not en_srt.exists():
        if final_srt_valid(subs):
            # Rescue/finished project: final.srt is authoritative, hybrid.srt is
            # only an input for generating it. Nothing to wait for.
            return

        log(
            f"Caption ready but en.srt not ready yet: "
            f"{folder.parent.name}/{folder.name}"
        )
        return

    if not youtube_srt.exists():
        return

    log(
        f"Running hybrid: "
        f"{folder.parent.name}/{folder.name}"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(HYBRID_SCRIPT),
            str(subs),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log(
                "HYBRID: " + line
            )

    if result.returncode != 0:
        raise RuntimeError(
            f"Hybrid failed exit={result.returncode}"
        )

    if not hybrid_srt.exists():
        raise RuntimeError(
            "Hybrid script finished but "
            "hybrid.srt was not created."
        )

    log(
        f"HYBRID SUCCESS: "
        f"{folder.parent.name}/{folder.name}"
    )


def process_one(
    youtube,
    folder,
    video_id,
):
    name = (
        f"{folder.parent.name}/{folder.name}"
    )

    log(
        f"Checking YouTube captions: "
        f"{name} video={video_id}"
    )

    track = find_best_caption_track(
        youtube,
        video_id,
    )

    if not track:
        log(
            f"WAITING: no usable caption track yet: "
            f"{name}"
        )
        return

    caption_id = track["id"]
    snippet = track.get(
        "snippet",
        {},
    )

    language = snippet.get(
        "language",
        "?",
    )

    kind = snippet.get(
        "trackKind",
        "?",
    )

    log(
        f"Caption found: "
        f"{name} "
        f"language={language} "
        f"kind={kind}"
    )

    output = (
        folder
        / "subs"
        / "youtube.srt"
    )

    download_caption(
        youtube,
        caption_id,
        output,
    )

    log(
        f"CAPTION DOWNLOADED: {output}"
    )

    run_hybrid(folder)


def main():
    try:
        targets = collect_targets()
        if not targets:
            return

        creds = load_credentials()

        youtube = build(
            "youtube",
            "v3",
            credentials=creds,
            cache_discovery=False,
        )

        log(
            f"Found {len(targets)} "
            f"caption target(s)."
        )

        for folder, video_id in targets:
            try:
                process_one(
                    youtube,
                    folder,
                    video_id,
                )

            except HttpError as e:
                status = getattr(
                    e.resp,
                    "status",
                    None,
                )

                log(
                    f"YouTube API error "
                    f"{folder.name}: "
                    f"HTTP {status} {e}",
                    "WARN",
                )

            except Exception as e:
                log(
                    f"FAILED "
                    f"{folder.parent.name}/"
                    f"{folder.name}: {e}",
                    "ERROR",
                )

    except Exception as e:
        log(
            f"FATAL: {e}",
            "ERROR",
        )


if __name__ == "__main__":
    with (BASE / ".caption_fetch.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(0)
        with paths_lock():
            main()
