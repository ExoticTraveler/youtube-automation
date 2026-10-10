#!/usr/bin/env python3

from pipeline_state import stable, paths_lock
import os
import fcntl
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

# launchd / system Python から起動されても
# Homebrew ffmpeg を確実に見つけられるようにする
os.environ["PATH"] = (
    "/opt/homebrew/bin:"
    "/usr/local/bin:"
    + os.environ.get("PATH", "")
)

BASE = Path.home() / "YT_QUEUE"

WATCH_DIRS = [
    BASE / "prepared",
    BASE / "done",
]

LOCK_FILE = BASE / ".sub_autogen.lock"

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "sub_autogen.log"

DEMUCS_BIN = str(Path.home() / ".pyenv/shims/demucs")
WHISPER_BIN = str(Path.home() / ".pyenv/shims/whisper")
FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg"

DEMUCS_MODEL = "mdx_extra_q"
WHISPER_MODEL = "medium"
LANG = "English"

VIDEO_EXTS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
}


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"

    print(line, flush=True)

    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def preflight():
    checks = [
        ("Demucs", DEMUCS_BIN),
        ("Whisper", WHISPER_BIN),
        ("ffmpeg", FFMPEG_BIN),
    ]

    for name, path in checks:
        if not Path(path).exists():
            raise RuntimeError(
                f"{name} not found: {path}"
            )


def find_video(folder):
    for f in sorted(folder.iterdir()):
        if (
            f.is_file()
            and f.suffix.lower() in VIDEO_EXTS
        ):
            return f

    return None


def final_srt_valid(subs_dir):
    """True when subs/final.srt exists and passes final_subtitle_autogen validation."""
    try:
        from final_subtitle_autogen import final_status
        return final_status(subs_dir) == "DONE"
    except Exception:
        return False


def collect_targets():
    targets = []

    for root in WATCH_DIRS:
        root.mkdir(
            parents=True,
            exist_ok=True,
        )

        for folder in root.iterdir():
            if not folder.is_dir():
                continue

            if folder.name.startswith("."):
                continue

            video = find_video(folder)

            if not video:
                continue

            subs_dir = folder / "subs"
            en_srt = subs_dir / "en.srt"

            # 有効な final.srt があれば字幕は完了扱い
            # (final.srt は完成後の正本。Demucs/Whisper は走らせない)
            if final_srt_valid(subs_dir):
                done = folder / ".sub_done"

                if not done.exists():
                    done.write_text(
                        "ok\n",
                        encoding="utf-8",
                    )

                continue

            # en.srt が既にあれば完了扱い
            if (
                en_srt.exists()
                and en_srt.stat().st_size > 0
            ):
                done = folder / ".sub_done"

                if not done.exists():
                    done.write_text(
                        "ok\n",
                        encoding="utf-8",
                    )

                continue

            # 失敗済み案件は自動再試行しない
            # 人間が .sub_failed を消したら再開
            if (folder / ".sub_failed").exists():
                continue

            if root == BASE / "prepared" and not stable(folder, video, log):
                continue

            targets.append(
                (folder, video)
            )

    targets.sort(
        key=lambda x: x[0].stat().st_mtime
    )

    return targets


def prepare_workdir(folder):
    work = folder / ".sub_work"
    demucs_out = work / "demucs"
    whisper_out = work / "whisper"

    work.mkdir(
        parents=True,
        exist_ok=True,
    )

    demucs_out.mkdir(
        parents=True,
        exist_ok=True,
    )

    whisper_out.mkdir(
        parents=True,
        exist_ok=True,
    )

    return work, demucs_out, whisper_out


def get_cached_vocals(folder):
    vocals = (
        folder
        / ".sub_work"
        / "vocals.wav"
    )

    if (
        vocals.exists()
        and vocals.stat().st_size > 0
    ):
        return vocals

    return None


def run_demucs(folder, video):
    work, demucs_out, _ = prepare_workdir(
        folder
    )

    cached = get_cached_vocals(folder)

    if cached:
        log(
            f"Using cached vocals.wav: {cached}"
        )
        return cached

    log(
        f"Running demucs: {video}"
    )

    result = subprocess.run(
        [
            DEMUCS_BIN,
            "-n",
            DEMUCS_MODEL,
            "-o",
            str(demucs_out),
            str(video),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )

    demucs_log = work / "demucs.log"

    demucs_log.write_text(
        result.stdout or "",
        encoding="utf-8",
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Demucs failed exit={result.returncode}. "
            f"See {demucs_log}"
        )

    generated = (
        demucs_out
        / DEMUCS_MODEL
        / video.stem
        / "vocals.wav"
    )

    if not generated.exists():
        raise RuntimeError(
            "Demucs finished but vocals.wav "
            f"was not found. See {demucs_log}"
        )

    cached = work / "vocals.wav"

    shutil.move(
        str(generated),
        str(cached),
    )

    log(
        f"Demucs OK: {cached}"
    )

    return cached


def run_whisper(folder, vocals):
    subs_dir = folder / "subs"

    subs_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    out_srt = subs_dir / "en.srt"

    work, _, whisper_out = prepare_workdir(
        folder
    )

    # Whisper出力だけ消す
    # vocals.wav は絶対残す
    if whisper_out.exists():
        shutil.rmtree(
            whisper_out,
            ignore_errors=True,
        )

    whisper_out.mkdir(
        parents=True,
        exist_ok=True,
    )

    log(
        f"Running whisper: {vocals}"
    )

    env = os.environ.copy()

    # 念押しで ffmpeg の場所を PATH 先頭に固定
    env["PATH"] = (
        "/opt/homebrew/bin:"
        "/usr/local/bin:"
        + env.get("PATH", "")
    )

    result = subprocess.run(
        [
            WHISPER_BIN,
            str(vocals),
            "--model",
            WHISPER_MODEL,
            "--language",
            LANG,
            "--task",
            "transcribe",
            "--output_dir",
            str(whisper_out),
            "--output_format",
            "srt",
            "--verbose",
            "False",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    whisper_log = work / "whisper.log"

    whisper_log.write_text(
        result.stdout or "",
        encoding="utf-8",
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Whisper failed exit={result.returncode}. "
            f"vocals.wav preserved at {vocals}. "
            f"See {whisper_log}"
        )

    generated_srts = list(
        whisper_out.glob("*.srt")
    )

    if not generated_srts:
        raise RuntimeError(
            "Whisper finished but no SRT was generated. "
            f"vocals.wav preserved at {vocals}. "
            f"See {whisper_log}"
        )

    if len(generated_srts) > 1:
        names = ", ".join(
            f.name for f in generated_srts
        )

        raise RuntimeError(
            "Whisper generated multiple SRT files: "
            f"{names}. "
            f"vocals.wav preserved at {vocals}"
        )

    generated = generated_srts[0]

    if generated.stat().st_size == 0:
        raise RuntimeError(
            "Whisper generated an empty SRT. "
            f"vocals.wav preserved at {vocals}. "
            f"See {whisper_log}"
        )

    shutil.move(
        str(generated),
        str(out_srt),
    )

    log(
        f"Whisper OK: {out_srt}"
    )

    return out_srt


def mark_failed(folder, error):
    failed = folder / ".sub_failed"

    failed.write_text(
        str(error) + "\n",
        encoding="utf-8",
    )

    log(
        f"FAILED: "
        f"{folder.parent.name}/{folder.name} "
        f"reason={error}",
        "ERROR",
    )

    log(
        "Automatic retry disabled for this project. "
        "Fix the cause, remove .sub_failed, then retry.",
        "WARN",
    )


def cleanup_after_success(folder):
    work = folder / ".sub_work"

    if work.exists():
        shutil.rmtree(
            work,
            ignore_errors=True,
        )

        log(
            f"Cleaned subtitle work files: {work}"
        )


def process(folder, video):
    subs_dir = folder / "subs"

    subs_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    vocals = run_demucs(
        folder,
        video,
    )

    run_whisper(
        folder,
        vocals,
    )

    (folder / ".sub_done").write_text(
        "ok\n",
        encoding="utf-8",
    )

    failed = folder / ".sub_failed"

    if failed.exists():
        failed.unlink()

    cleanup_after_success(folder)

    log(
        f"SUCCESS: "
        f"{folder.parent.name}/{folder.name} "
        f"-> subs/en.srt"
    )


def process_all():
    preflight()

    targets = collect_targets()

    if not targets:
        log(
            "No subtitle targets in prepared/ or done."
        )
        log(
            "FINISHED. success=0 failed=0"
        )
        return

    log(
        f"Found {len(targets)} subtitle target(s)."
    )

    success = 0
    failed = 0

    # 対象一覧は開始時点で固定
    # 失敗案件を同一run内で再試行しない
    for folder, video in targets:
        log(
            f"Picked: "
            f"{folder.parent.name}/{folder.name} "
            f"/ {video.name}"
        )

        try:
            process(
                folder,
                video,
            )

            success += 1

        except Exception as e:
            failed += 1

            mark_failed(
                folder,
                e,
            )

    log(
        f"FINISHED. "
        f"success={success} "
        f"failed={failed}"
    )


def main():
    LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with LOCK_FILE.open("w") as lockf:
        try:
            fcntl.flock(
                lockf,
                fcntl.LOCK_EX
                | fcntl.LOCK_NB,
            )

        except BlockingIOError:
            log("Already running")
            return

        with paths_lock():
            process_all()


if __name__ == "__main__":
    main()
