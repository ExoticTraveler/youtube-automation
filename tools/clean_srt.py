#!/usr/bin/env python3
from pathlib import Path
import re
import sys

MAX_LINE = 42
MAX_BLOCK = 80

def parse_time(t):
    h, m, s = t.replace(',', '.').split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)

def format_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02}:{m:02}:{s:06.3f}".replace('.', ',')

def split_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []

    for s in sentences:
        if len(s) <= MAX_BLOCK:
            chunks.append(s)
        else:
            words = s.split()
            cur = []
            for w in words:
                test = " ".join(cur + [w])
                if len(test) > MAX_BLOCK and cur:
                    chunks.append(" ".join(cur))
                    cur = [w]
                else:
                    cur.append(w)
            if cur:
                chunks.append(" ".join(cur))

    return chunks

def split_lines(text):
    words = text.split()
    lines = []
    cur = []

    for w in words:
        test = " ".join(cur + [w])
        if len(test) > MAX_LINE and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)

    if cur:
        lines.append(" ".join(cur))

    if len(lines) <= 2:
        return lines

    return [lines[0], " ".join(lines[1:])]

def process_block(timing, text):
    start, end = timing.split(" --> ")
    t0 = parse_time(start)
    t1 = parse_time(end)
    chunks = split_text(text)

    if len(chunks) <= 1:
        return [(timing, split_lines(text))]

    dur = (t1 - t0) / len(chunks)
    out = []

    for i, chunk in enumerate(chunks):
        s = t0 + dur * i
        e = s + dur
        out.append((format_time(s) + " --> " + format_time(e), split_lines(chunk)))

    return out

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 clean_srt.py input.srt")
        sys.exit(1)

    inp = Path(sys.argv[1]).expanduser()
    out = inp.with_name(inp.stem + "_clean.srt")

    raw = inp.read_text(encoding="utf-8").strip()
    blocks = re.split(r"\n\s*\n", raw)

    final = []
    count = 1

    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue

        timing = lines[1]
        text = " ".join(lines[2:])

        for new_timing, text_lines in process_block(timing, text):
            final.append(str(count))
            final.append(new_timing)
            final.extend(text_lines)
            final.append("")
            count += 1

    out.write_text("\n".join(final), encoding="utf-8")
    print("DONE:", out)

if __name__ == "__main__":
    main()
