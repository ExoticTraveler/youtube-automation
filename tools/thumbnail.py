#!/usr/bin/env python3
"""Exotic Traveler thumbnail frame extractor and production asset-package CLI.

Legacy ``--project PATH --extract-only`` behavior remains available. Normal
production use creates A/B/C/D branded candidates plus five separate real-person
PNG cutouts for human selection; it does not upload or choose a winner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mkv", ".webm"}
GENERATED_RE = re.compile(r"^(periodic|scene)_\d+\.jpg$")


@dataclass
class Candidate:
    file: str
    source: str
    timestamp_seconds: float
    timestamp: str
    brightness: float
    contrast: float
    sharpness: float
    duplicate_of: str | None = None
    rejected_reason: str | None = None
    deterministic_score: float | None = None


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"Required executable not found: {name}")
    return path


def discover_video(project: Path) -> Path:
    videos = sorted(
        p for p in project.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if len(videos) != 1:
        names = ", ".join(p.name for p in videos) or "none"
        raise SystemExit(f"Expected exactly one project-root video; found: {names}")
    return videos[0]


def probe_duration(ffprobe: str, video: Path) -> float:
    result = run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture=True,
    )
    return float(result.stdout.strip())


def timestamp_text(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def extract_at(ffmpeg: str, video: Path, seconds: float, output: Path) -> None:
    run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(video),
            "-frames:v", "1", "-q:v", "2", "-vf", "scale=1280:720:flags=lanczos", "-y", str(output),
        ]
    )


def extract_scene_times(ffmpeg: str, video: Path, threshold: float, minimum_gap: float) -> list[float]:
    # showinfo emits pts_time for scene-change frames. fps prevents bursts around edits.
    vf = f"select='gt(scene,{threshold})',fps=1/{minimum_gap},showinfo"
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video), "-vf", vf, "-an", "-f", "null", "-"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [float(value) for value in re.findall(r"pts_time:([0-9.]+)", result.stderr)]


def decode_small_gray(ffmpeg: str, image: Path) -> np.ndarray:
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(image), "-vf", "scale=96:54,format=gray",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        stdout=subprocess.PIPE,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape((54, 96)).astype(np.float32)


def dhash(gray: np.ndarray) -> int:
    # Reduce to 9x8 by block means without another dependency.
    y_edges = np.linspace(0, gray.shape[0], 9, dtype=int)
    x_edges = np.linspace(0, gray.shape[1], 10, dtype=int)
    tiny = np.empty((8, 9), dtype=np.float32)
    for y in range(8):
        for x in range(9):
            tiny[y, x] = gray[y_edges[y]:y_edges[y + 1], x_edges[x]:x_edges[x + 1]].mean()
    bits = tiny[:, 1:] > tiny[:, :-1]
    value = 0
    for bit in bits.flat:
        value = (value << 1) | int(bit)
    return value


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def image_metrics(ffmpeg: str, image: Path) -> tuple[float, float, float, int]:
    gray = decode_small_gray(ffmpeg, image)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    # Mean squared spatial gradient: inexpensive sharpness proxy, not semantic quality.
    gx = np.diff(gray, axis=1)
    gy = np.diff(gray, axis=0)
    sharpness = float((np.mean(gx * gx) + np.mean(gy * gy)) / 2.0)
    return brightness, contrast, sharpness, dhash(gray)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", nargs="?", help="Project ID or project path")
    parser.add_argument("--project", type=Path, help="Backward-compatible explicit project directory")
    parser.add_argument("--output-root", type=Path, help="Optional safe test destination instead of <project>/thumbnail")
    parser.add_argument("--extract-only", action="store_true", help="Run the legacy deterministic frame extractor only")
    parser.add_argument("--run", action="store_true", help="Build and atomically publish a production asset package")
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs and report calls/output without writing or calling APIs")
    parser.add_argument("--setup", action="store_true", help="Create the pinned local segmentation/image runtime")
    parser.add_argument("--plan-file", type=Path, help="Use a reviewed plan JSON instead of the paid planning call (testing/recovery)")
    parser.add_argument("--planning-model", default="gpt-4.1-mini", help="OpenAI model for the single structured planning call")
    parser.add_argument("--vetting-model", default="gpt-4.1", help="OpenAI vision model for the single creator-identity frame-vetting call")
    parser.add_argument("--image-model", default="gpt-image-2", help="OpenAI image-edit model used for background recomposition")
    parser.add_argument("--image-quality", default="high", choices=("low", "medium", "high"))
    parser.add_argument("--image-mode", choices=("api", "local"), default="api", help="api = image-edit recomposition of real frames (production); local = explicit DEGRADED crop/grade mode")
    parser.add_argument("--country-name", help="Explicit reviewed country-name override")
    parser.add_argument("--country-code", help="Explicit ISO alpha-2 flag override")
    parser.add_argument("--interval", type=float, default=10.0, help="Periodic sample interval in seconds")
    parser.add_argument("--scene-threshold", type=float, default=0.38)
    parser.add_argument("--scene-gap", type=float, default=8.0, help="Minimum seconds between scene samples")
    parser.add_argument("--max-scene", type=int, default=60)
    return parser.parse_args()


def extract_main(args=None) -> int:
    args = parse_args() if args is None else args
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"Project directory does not exist: {project}")
    ffmpeg = require_binary("ffmpeg")
    ffprobe = require_binary("ffprobe")
    video = discover_video(project)
    duration = probe_duration(ffprobe, video)
    thumbnail_root = (args.output_root or (project / "thumbnail")).expanduser().resolve()
    frames_dir = thumbnail_root / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Reruns replace only files whose names are owned by this tool.
    for path in frames_dir.iterdir():
        if path.is_file() and GENERATED_RE.match(path.name):
            path.unlink()

    samples: list[tuple[str, float]] = []
    periodic_times = np.arange(max(2.0, args.interval / 2.0), duration - 1.0, args.interval)
    samples.extend(("periodic", float(t)) for t in periodic_times)

    scene_times = extract_scene_times(ffmpeg, video, args.scene_threshold, args.scene_gap)
    if len(scene_times) > args.max_scene:
        indexes = np.linspace(0, len(scene_times) - 1, args.max_scene, dtype=int)
        scene_times = [scene_times[i] for i in indexes]
    # Avoid scene samples that land within two seconds of periodic samples.
    samples.extend(
        ("scene", t) for t in scene_times if min(abs(t - p) for p in periodic_times) > 2.0
    )
    samples.sort(key=lambda item: item[1])

    candidates: list[Candidate] = []
    hashes: list[tuple[str, int]] = []
    counters = {"periodic": 0, "scene": 0}
    for source, seconds in samples:
        counters[source] += 1
        filename = f"{source}_{counters[source]:03d}.jpg"
        path = frames_dir / filename
        extract_at(ffmpeg, video, seconds, path)
        brightness, contrast, sharpness, image_hash = image_metrics(ffmpeg, path)
        candidate = Candidate(
            file=filename,
            source=source,
            timestamp_seconds=round(seconds, 3),
            timestamp=timestamp_text(seconds),
            brightness=round(brightness, 2),
            contrast=round(contrast, 2),
            sharpness=round(sharpness, 2),
        )
        if brightness < 22:
            candidate.rejected_reason = "extremely_dark"
        elif contrast < 10:
            candidate.rejected_reason = "very_low_contrast"
        elif sharpness < 18:
            candidate.rejected_reason = "extremely_soft"
        else:
            for prior_name, prior_hash in hashes:
                if hamming(image_hash, prior_hash) <= 4:
                    candidate.duplicate_of = prior_name
                    candidate.rejected_reason = "near_duplicate"
                    break
        if not candidate.rejected_reason:
            hashes.append((filename, image_hash))
            exposure = max(0.0, 1.0 - abs(brightness - 120.0) / 120.0)
            candidate.deterministic_score = round(
                0.45 * math.log1p(sharpness) + 0.35 * math.log1p(contrast) + 0.20 * exposure,
                4,
            )
        candidates.append(candidate)

    manifest = {
        "tool": "thumbnail.py",
        "scope": "deterministic extraction/filtering only; scores are not semantic ranking",
        "project": str(project),
        "video": str(video),
        "video_size": video.stat().st_size,
        "duration_seconds": duration,
        "settings": {
            "interval": args.interval,
            "scene_threshold": args.scene_threshold,
            "scene_gap": args.scene_gap,
            "max_scene": args.max_scene,
        },
        "counts": {
            "extracted": len(candidates),
            "accepted": sum(not c.rejected_reason for c in candidates),
            "rejected": sum(bool(c.rejected_reason) for c in candidates),
        },
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    (frames_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["counts"], indent=2))
    print(f"Manifest: {frames_dir / 'manifest.json'}")
    return 0


def main() -> int:
    args = parse_args()
    if args.setup:
        from thumbnail_production import setup
        setup(sys.executable)
        return 0
    value = args.project or args.project_id
    if not value:
        raise SystemExit("Provide PROJECT_ID/path, --project PATH, or --setup")
    if args.extract_only:
        args.project = Path(value)
        return extract_main(args)
    if not (args.run or args.dry_run):
        raise SystemExit("Choose --run, --dry-run, or --extract-only")
    venv_python = Path(__file__).with_name(".thumbnail_venv") / "bin" / "python"
    if venv_python.exists() and Path(sys.prefix).resolve() != venv_python.parent.parent.resolve():
        import os
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    from thumbnail_production import resolve_project, run_production
    try:
        args.project = resolve_project(value)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        return run_production(args, extract_main)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
