#!/usr/bin/env python3
from pathlib import Path
import re
import sys

MAX_LINE = 42

BASE = Path.home() / "YT_QUEUE"

WATCH_DIRS = [
    BASE / "prepared",
    BASE / "done",
]


def parse_time(t):
    t = t.strip().replace(",", ".")
    parts = t.split(":")

    if len(parts) == 3:
        h, m, s = parts

    elif len(parts) == 2:
        h = 0
        m, s = parts

    else:
        raise ValueError(
            "bad time: " + t
        )

    return (
        int(h) * 3600
        + int(m) * 60
        + float(s)
    )


def fmt_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60

    return (
        f"{h:02}:{m:02}:{s:06.3f}"
        .replace(".", ",")
    )


def parse_sub(path):
    raw = (
        Path(path)
        .read_text(
            encoding="utf-8-sig"
        )
        .strip()
    )

    blocks = re.split(
        r"\n\s*\n",
        raw
    )

    out = []

    for block in blocks:

        lines = [
            x.rstrip()
            for x in block.strip().splitlines()
            if x.strip()
        ]

        if len(lines) < 2:
            continue

        if (
            len(lines) >= 3
            and " --> " in lines[1]
        ):

            timing = lines[1].strip()

            start_s, end_s = (
                timing.split(" --> ")
            )

            text_lines = lines[2:]

        else:

            m = re.match(
                r"^\s*([0-9:.]+)\s*,\s*([0-9:.]+)\s*$",
                lines[0]
            )

            if not m:
                continue

            start_s = m.group(1)
            end_s = m.group(2)

            timing = (
                fmt_time(
                    parse_time(start_s)
                )
                + " --> "
                + fmt_time(
                    parse_time(end_s)
                )
            )

            text_lines = lines[1:]

        text = clean_text(
            " ".join(
                x.strip()
                for x in text_lines
                if x.strip()
            )
        )

        out.append({
            "start": parse_time(start_s),
            "end": parse_time(end_s),
            "time": timing,
            "text": text,
            "words": split_words(text),
        })

    return out


def clean_text(t):
    return re.sub(
        r"\s+",
        " ",
        t.replace(
            "\u00a0",
            " "
        )
    ).strip()


def split_words(t):
    return re.findall(
        r"\S+",
        clean_text(t)
    )


def overlap(a0, a1, b0, b1):
    return max(
        0.0,
        min(a1, b1)
        - max(a0, b0)
    )


def wrap_words(words):
    lines = []
    cur = []

    for w in words:

        test = " ".join(
            cur + [w]
        )

        if (
            len(test) > MAX_LINE
            and cur
        ):

            lines.append(
                " ".join(cur)
            )

            cur = [w]

        else:
            cur.append(w)

    if cur:
        lines.append(
            " ".join(cur)
        )

    if len(lines) <= 2:
        return lines

    return [
        lines[0],
        " ".join(lines[1:])
    ]


def flatten_whisper(blocks):
    tokens = []

    for b in blocks:

        dur = max(
            0.001,
            b["end"] - b["start"]
        )

        ws = b["words"]

        if not ws:
            continue

        step = dur / len(ws)

        for i, w in enumerate(ws):

            tokens.append({
                "word": w,
                "time": (
                    b["start"]
                    + step * (i + 0.5)
                ),
                "used": False,
            })

    return tokens


def make_hybrid(
    youtube_blocks,
    whisper_blocks
):

    tokens = flatten_whisper(
        whisper_blocks
    )

    result = []
    report = []

    no_overlap_count = 0
    fallback_youtube_count = 0
    empty_text_count = 0
    used_word_count = 0

    cursor = 0

    for i, y in enumerate(
        youtube_blocks
    ):

        y0 = y["start"]
        y1 = y["end"]

        chosen = []

        while (
            cursor < len(tokens)
            and tokens[cursor]["time"] < y0
        ):
            cursor += 1

        scan = cursor

        while (
            scan < len(tokens)
            and tokens[scan]["time"] <= y1
        ):

            if not tokens[scan]["used"]:
                chosen.append(scan)

            scan += 1

        if chosen:

            for idx in chosen:
                tokens[idx]["used"] = True

            words = [
                tokens[idx]["word"]
                for idx in chosen
            ]

            used_word_count += len(words)

        else:

            no_overlap_count += 1
            fallback_youtube_count += 1

            words = split_words(
                y["text"]
            )

            report.append(
                "NO_WHISPER_WORDS_USE_YOUTUBE "
                f"block={i+1} "
                f"yt_time={y['time']} "
                f"yt_text={y['text']}"
            )

        if not words:

            empty_text_count += 1

            words = split_words(
                y["text"]
            )

        result.append({
            "time": y["time"],
            "lines": wrap_words(words),
        })

    stats = {
        "youtube_blocks": len(
            youtube_blocks
        ),
        "whisper_blocks": len(
            whisper_blocks
        ),
        "whisper_words_total": len(
            tokens
        ),
        "whisper_words_used": (
            used_word_count
        ),
        "no_overlap_count": (
            no_overlap_count
        ),
        "fallback_youtube_count": (
            fallback_youtube_count
        ),
        "empty_text_count": (
            empty_text_count
        ),
    }

    return result, stats, report


def write_srt(path, blocks):
    out = []

    for i, b in enumerate(
        blocks,
        1
    ):

        out.append(str(i))
        out.append(b["time"])
        out.extend(b["lines"])
        out.append("")

    Path(path).write_text(
        "\n".join(out),
        encoding="utf-8"
    )


def write_report(
    path,
    stats,
    report
):

    lines = []

    for k, v in stats.items():

        lines.append(
            k + ": " + str(v)
        )

    lines.append("")
    lines.append("DETAILS:")
    lines.extend(report)

    Path(path).write_text(
        "\n".join(lines),
        encoding="utf-8"
    )


def process_subs_folder(folder):

    folder = Path(folder).expanduser()

    youtube = (
        folder / "youtube.srt"
    )

    whisper = (
        folder / "en.srt"
    )

    output = (
        folder / "hybrid.srt"
    )

    report = (
        folder / "hybrid_report.txt"
    )

    if not whisper.exists():
        print(
            "SKIP no en.srt:",
            folder
        )
        return False

    if not youtube.exists():
        print(
            "SKIP no youtube.srt:",
            folder
        )
        return False

    if output.exists():
        print(
            "SKIP hybrid exists:",
            folder
        )
        return False

    print()
    print(
        "PROCESS:",
        folder
    )

    youtube_blocks = parse_sub(
        youtube
    )

    whisper_blocks = parse_sub(
        whisper
    )

    hybrid, stats, details = (
        make_hybrid(
            youtube_blocks,
            whisper_blocks
        )
    )

    write_srt(
        output,
        hybrid
    )

    write_report(
        report,
        stats,
        details
    )

    print(
        "DONE:",
        output
    )

    print(
        "REPORT:",
        report
    )

    print(stats)

    return True


def collect_all_targets():

    targets = []

    for root in WATCH_DIRS:

        root.mkdir(
            parents=True,
            exist_ok=True
        )

        for project in root.iterdir():

            if not project.is_dir():
                continue

            if project.name.startswith("."):
                continue

            subs = project / "subs"

            if not subs.is_dir():
                continue

            en = subs / "en.srt"
            yt = subs / "youtube.srt"
            hybrid = subs / "hybrid.srt"

            if not en.exists():
                continue

            if not yt.exists():
                continue

            if hybrid.exists():
                continue

            targets.append(
                (
                    project.stat().st_mtime,
                    subs,
                )
            )

    targets.sort(
        key=lambda x: x[0]
    )

    return [
        subs
        for _, subs in targets
    ]


def process_all():

    targets = collect_all_targets()

    if not targets:

        print(
            "No hybrid targets "
            "in prepared/ or done/."
        )

        return

    print(
        "Found",
        len(targets),
        "hybrid target(s)."
    )

    success = 0
    failed = 0

    for folder in targets:

        try:

            if process_subs_folder(
                folder
            ):
                success += 1

        except Exception as e:

            failed += 1

            print(
                "FAILED:",
                folder,
                "reason=",
                str(e)
            )

    print()
    print(
        "FINISHED. success="
        + str(success)
        + " failed="
        + str(failed)
    )


def main():

    # 引数なし
    # prepared/ + done/ を全件自動処理
    if len(sys.argv) == 1:

        process_all()
        return

    # subsフォルダ単体指定
    if len(sys.argv) == 2:

        process_subs_folder(
            Path(
                sys.argv[1]
            ).expanduser()
        )

        return

    # 旧来の3ファイル直接指定も維持
    if len(sys.argv) == 4:

        youtube = Path(
            sys.argv[1]
        ).expanduser()

        whisper = Path(
            sys.argv[2]
        ).expanduser()

        output = Path(
            sys.argv[3]
        ).expanduser()

        report = output.with_name(
            output.stem
            + "_report.txt"
        )

        youtube_blocks = parse_sub(
            youtube
        )

        whisper_blocks = parse_sub(
            whisper
        )

        hybrid, stats, details = (
            make_hybrid(
                youtube_blocks,
                whisper_blocks
            )
        )

        write_srt(
            output,
            hybrid
        )

        write_report(
            report,
            stats,
            details
        )

        print(
            "DONE:",
            output
        )

        print(
            "REPORT:",
            report
        )

        print(stats)

        return

    print("Usage:")
    print()
    print(
        "  python3 "
        "make_hybrid_srt.py"
    )
    print()
    print(
        "  python3 "
        "make_hybrid_srt.py "
        "~/YT_QUEUE/done/IRAQ2/subs"
    )
    print()
    print(
        "  python3 "
        "make_hybrid_srt.py "
        "youtube.srt en.srt hybrid.srt"
    )

    sys.exit(1)


if __name__ == "__main__":
    main()
