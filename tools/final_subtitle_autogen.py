#!/usr/bin/env python3
"""Conservative text-only corrections; YouTube cue boundaries are immutable."""
import argparse
import bisect
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import subprocess
import sys
import traceback
from datetime import datetime

BASE = Path.home() / 'YT_QUEUE'
NARRATION_ASSET_DIR = (Path.home() / 'Documents/Obsidian Vault/Knowledge Factory/YouTube'
                       / 'Narration Sources')
# Explicit project-family routing.  Keep this deliberately deterministic: do
# not infer a country from transcript text or search the filesystem broadly.
# The legacy Iran file is retained as a conservative fallback until a clean
# Iran_2026.md archive exists in Narration Sources.
COUNTRY_NARRATION_SOURCES = {
    'Iran': {
        'project_ids': ('IRAN',),
        'paths': ('Iran_2026.md',),
        'legacy_paths': ('done/IRAN2/subs/Iran_2026_Narration_Master.md',),
    },
    # Add future families here, for example:
    # 'Afghanistan': {'project_ids': ('AFGHANISTAN', 'AFG'),
    #                 'paths': ('Afghanistan_2026.md',), 'legacy_paths': ()},
}
SOURCES = ('en.srt', 'youtube.srt', 'hybrid.srt')
TIME = re.compile(r'^(\d{2,}:\d{2}:\d{2},\d{3}) --> (\d{2,}:\d{2}:\d{2},\d{3})(?:[ \t]+[^\n]+)?$')
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
MAX_GAP = 800  # Never run quadratic sequence matching on an entire long film.

def parse_srt(text, allow_legacy=False):
    blocks = []
    for part in re.split(r'\n[ \t]*\n', text.lstrip('\ufeff').replace('\r\n', '\n').strip()):
        lines = part.splitlines()
        # Older YouTube exports use h:mm:ss.mmm,h:mm:ss.mmm without cue IDs.
        # Convert notation only; every millisecond is copied from YouTube.
        if allow_legacy and lines:
            legacy = re.fullmatch(r"(\d+):(\d{2}):(\d{2})\.(\d{3}),(\d+):(\d{2}):(\d{2})\.(\d{3})", lines[0])
            if legacy:
                h, m, sec, milli, eh, em, es, efrac = legacy.groups()
                timing = f"{int(h):02d}:{m}:{sec},{milli} --> {int(eh):02d}:{em}:{es},{efrac}"
                lines = [str(len(blocks)+1), timing] + lines[1:]
        if len(lines) < 3 or not lines[0].strip().isdigit() or not TIME.fullmatch(lines[1]):
            raise ValueError('Invalid SRT cue structure')
        match = TIME.fullmatch(lines[1])
        def ms(t):
            h, m, s, milli = map(int, re.split('[:,]', t))
            if m >= 60 or s >= 60: raise ValueError('Invalid time field')
            return ((h * 60 + m) * 60 + s) * 1000 + milli
        if ms(match[1]) > ms(match[2]) or not '\n'.join(lines[2:]).strip():
            raise ValueError('Invalid cue duration/text')
        blocks.append({'timing': lines[1], 'text': '\n'.join(lines[2:])})
    if not blocks: raise ValueError('Empty SRT')
    return blocks

def validate(final, youtube):
    if len(final) != len(youtube): raise ValueError('Cue count differs from YouTube')
    for i, (out, src) in enumerate(zip(final, youtube), 1):
        if out['timing'] != src['timing']:
            raise ValueError(f'YouTube timing mismatch at cue {i}')
    return True

def words(blocks):
    tokens, positions = [], []
    for i, block in enumerate(blocks):
        for m in WORD.finditer(block['text']):
            tokens.append(m.group().casefold().replace('’', "'"))
            positions.append((i, m.start(), m.end(), m.group()))
    return tokens, positions

def anchors(a, b):
    """Unique 4-word anchors + longest increasing subsequence: O(n log n)."""
    def unique(seq):
        found = {}
        for i in range(len(seq) - 3):
            k = tuple(seq[i:i+4]); found[k] = i if k not in found else None
        return found
    ua, ub = unique(a), unique(b)
    pairs = sorted((i, ub[k]) for k, i in ua.items() if i is not None and ub.get(k) is not None)
    tails, indices, previous = [], [], []
    for index, (_, j) in enumerate(pairs):
        level = bisect.bisect_left(tails, j)
        previous.append(indices[level-1] if level else -1)
        if level == len(tails): tails.append(j); indices.append(index)
        else: tails[level] = j; indices[level] = index
    chain = []
    index = indices[-1] if indices else -1
    while index >= 0:
        chain.append(pairs[index]); index = previous[index]
    selected = []
    for i, j in reversed(chain):
        if not selected or (i >= selected[-1][0]+4 and j >= selected[-1][1]+4): selected.append((i, j))
    return selected

def corrections(a, b, source_positions, reference_positions, reference_blocks):
    equal, changes = {}, []
    ai = bi = 0
    for x, y in anchors(a, b) + [(len(a), len(b))]:
        if max(x-ai, y-bi) <= MAX_GAP:
            for tag, i, end, j, rend in difflib.SequenceMatcher(None, a[ai:x], b[bi:y], autojunk=False).get_opcodes():
                i += ai; end += ai; j += bi; rend += bi
                if tag == 'equal': equal.update((i+k, j+k) for k in range(end-i))
                elif tag == 'replace': changes.append((i, end, j, rend))
        if x < len(a): equal.update((x+k, y+k) for k in range(4))
        ai, bi = x+4, y+4
    result = {}
    for i, end, j, rend in changes:
        # No insertions, deletions, cross-cue replacement, or unanchored edges.
        if not (1 <= end-i <= 3 and 1 <= rend-j <= 3): continue
        if source_positions[i][0] != source_positions[end-1][0]: continue
        if any(equal.get(i-k) != j-k for k in (1, 2)): continue
        if any(equal.get(end+k) != rend+k for k in (0, 1)): continue
        old, new = ''.join(a[i:end]), ''.join(b[j:rend])
        if difflib.SequenceMatcher(None, old, new).ratio() < .55: continue
        if not .5 <= len(new)/max(1, len(old)) <= 1.5: continue
        # Avoid inventing different numbers or mixing reference cue punctuation.
        if any(t.isdigit() for t in a[i:end]+b[j:rend]): continue
        first, last = reference_positions[j], reference_positions[rend-1]
        if first[0] == last[0]:
            text = reference_blocks[first[0]]['text'][first[1]:last[2]]
        else:
            text = ' '.join(p[3] for p in reference_positions[j:rend])
        result[(i, end)] = (tuple(b[j:rend]), text)
    return result

def generate(youtube, whisper, hybrid):
    yt, yp = words(youtube)
    en, ep = words(whisper); hy, hp = words(hybrid)
    wc = corrections(yt, en, yp, ep, whisper)
    hc = corrections(yt, hy, yp, hp, hybrid)
    edits = {}
    for span, (tokens, text) in wc.items():
        if span not in hc or hc[span][0] != tokens: continue
        first, last = yp[span[0]], yp[span[1]-1]
        edits.setdefault(first[0], []).append((first[1], last[2], text))
    final = [dict(block) for block in youtube]
    for i, items in edits.items():
        for start, end, text in sorted(items, reverse=True):
            final[i]['text'] = final[i]['text'][:start] + text + final[i]['text'][end:]
    validate(final, youtube)
    return final, sum(map(len, edits.values()))

def serialize(blocks):
    return '\n\n'.join(f"{i}\n{b['timing']}\n{b['text']}" for i, b in enumerate(blocks, 1)) + '\n'

def final_status(subs):
    try:
        if (subs/'final.srt').exists():
            final = parse_srt((subs/'final.srt').read_text(encoding='utf-8-sig'))
            if (subs/'youtube.srt').exists(): validate(final, parse_srt((subs/'youtube.srt').read_text(encoding='utf-8-sig'), allow_legacy=True))
            return 'DONE'
    except (OSError, ValueError, UnicodeError): pass
    if (subs/'.final_failed').exists(): return 'FAILED'
    return 'READY' if all((subs/n).is_file() and (subs/n).stat().st_size for n in SOURCES) else 'WAITING'

def log(message):
    line = f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}'
    print(line, flush=True)
    with (BASE/'logs/final_subtitle.log').open('a') as f: f.write(line+'\n')

def remove_fillers(text):
    """Text-only cleanup; retain filler-only cues because empty SRT is invalid."""
    pattern = r"(?:,[ \t]*)?(?<![\w'’-])(?:uh|um|erm|er)(?![\w'’-])(?:[ \t]*[,;:]|[ \t]*\.{2,}|[ \t]*…)*"
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
    if not matches:
        return text
    leading = not WORD.search(text[:matches[0].start()])
    cleaned = re.sub(pattern, ' ', text, flags=re.IGNORECASE)
    cleaned = re.sub(r",?[ \t]*(?:\(\s*\)|\[\s*\])[ \t]*[,;:]?", ' ', cleaned)
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)
    cleaned = re.sub(r'\s+([,.;:!?])', r'\1', cleaned)
    cleaned = re.sub(r'^[\s,;:.!?…]+', '', cleaned).strip()
    cleaned = '\n'.join(line.strip() for line in cleaned.splitlines() if line.strip())
    if not WORD.search(cleaned):
        log('FINAL filler-only cue retained: empty text is invalid')
        return text
    if leading:
        first = re.search(r'[^\W\d_]', cleaned)
        if first:
            i = first.start()
            cleaned = cleaned[:i] + cleaned[i].upper() + cleaned[i+1:]
    return cleaned

def _country_source(project):
    project_id = project.name.upper()
    for country, config in COUNTRY_NARRATION_SOURCES.items():
        if not any(project_id.startswith(prefix) for prefix in config['project_ids']):
            continue
        candidates = [NARRATION_ASSET_DIR / name for name in config.get('paths', ())]
        candidates.extend(BASE / path for path in config.get('legacy_paths', ()))
        for path in candidates:
            if path.is_symlink():
                raise ValueError(f'Country narration source must be a regular file: {path}')
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
                text = raw.decode('utf-8-sig')
            except (OSError, UnicodeError) as exc:
                log(f'REFERENCE LOW CONFIDENCE - IGNORED country={country} file={path} reason={type(exc).__name__}')
                continue
            if not raw or not text.strip():
                log(f'REFERENCE LOW CONFIDENCE - IGNORED country={country} empty={path}')
                continue
            log(f'REFERENCE FOUND country={country} project={project.name} file={path}')
            return path, raw, text
        log(f'REFERENCE COUNTRY SOURCE MISSING country={country} project={project.name}')
        return None
    log(f'REFERENCE COUNTRY UNMAPPED project={project.name}')
    return None


def narration_reference(project):
    # Preserve the existing per-project override before consulting Obsidian.
    for name in ('reference.md', 'narration.md'):
        path = project / name
        if path.is_file():
            try:
                raw = path.read_bytes()
                text = raw.decode('utf-8-sig')
            except (OSError, UnicodeError) as exc:
                if (project / '.reference_required').exists(): raise
                log(f'REFERENCE LOW CONFIDENCE - IGNORED unreadable={name} reason={type(exc).__name__}')
                return None
            log(f'REFERENCE FOUND project={project.name} file={name}')
            return path, raw, text
    # Production narration is a verbatim asset, addressed only by exact project ID.
    # Do not search semantically or fall back to Knowledge Factory summaries.
    path = NARRATION_ASSET_DIR / f'{project.name}.md'
    if path.is_symlink():
        raise ValueError(f'Obsidian narration source must be a regular file: {path}')
    if path.is_file():
        try:
            raw = path.read_bytes()
            text = raw.decode('utf-8-sig')
        except (OSError, UnicodeError) as exc:
            if (project / '.reference_required').exists(): raise
            log(f'REFERENCE LOW CONFIDENCE - IGNORED unreadable={path} reason={type(exc).__name__}')
            return None
        if not raw or not text.strip():
            if (project / '.reference_required').exists():
                raise ValueError(f'Obsidian narration source is empty: {path}')
            log(f'REFERENCE LOW CONFIDENCE - IGNORED empty={path}')
            return None
        log(f'REFERENCE FOUND project={project.name} file={path}')
        return path, raw, text
    # Country routing is explicit and comes only after all project-local and
    # exact-project sources.  Missing/ambiguous country material is optional.
    country = _country_source(project)
    if country is not None:
        return country
    log(f'REFERENCE NOT FOUND project={project.name} (ASR-consensus fallback)')
    if (project / '.reference_required').exists():
        raise ValueError('Reference explicitly required but no local or Obsidian narration source found')
    return None


def narration_paragraphs(text):
    # Strip simple Markdown presentation, not prose. Never execute reference content.
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
            lines.append('')
        elif not fenced:
            lines.append('' if line.lstrip().startswith('#') else re.sub(r'^\s*(?:>\s*|[-*+]\s+)', '', line))
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', '\n'.join(lines))
    text = text.replace('**', '').replace('__', '').replace('`', '')
    return [' '.join(p.split()) for p in re.split(r'\n\s*\n', text) if p.strip()]


def narration_index(tokens):
    index = {}
    for i in range(len(tokens) - 2):
        key = tuple(tokens[i:i+3])
        locations = index.setdefault(key, [])
        if len(locations) < 21: locations.append(i)
    return index


def narration_match(tokens, index, reference, cursor, sentence_ends):
    """Bounded paragraph match: exact lead-in, >=8 exact words, no insert/delete."""
    if not 8 <= len(reference) <= 160: return None
    starts = index.get(tuple(reference[:3]), [])
    if len(starts) > 20: return None  # Repeated lead-in is too ambiguous.
    candidates = []
    for start in starts:
        if start < cursor: continue
        best = None
        for size in range(max(8, len(reference)-3), len(reference)+4):
            end = start + size
            if end > len(tokens): continue
            matcher = difflib.SequenceMatcher(None, tokens[start:end], reference, autojunk=False)
            ops = matcher.get_opcodes()
            exact = sum(i2-i1 for tag, i1, i2, _, _ in ops if tag == 'equal')
            if matcher.ratio() < .82 or exact < 8: continue
            if any(tag in ('insert', 'delete') for tag, *_ in ops): continue
            if ops[-1][0] == 'replace' and end != len(tokens) and end not in sentence_ends:
                continue  # Do not truncate a split name/phrase to improve the score.
            safe = True
            for tag, i1, i2, j1, j2 in ops:
                if tag != 'replace': continue
                old, new = ''.join(tokens[start+i1:start+i2]), ''.join(reference[j1:j2])
                if (max(i2-i1, j2-j1) > 3 or
                    any(any(c.isdigit() for c in w) for w in tokens[start+i1:start+i2]+reference[j1:j2]) or
                    difflib.SequenceMatcher(None, old, new).ratio() < .55):
                    safe = False; break
            if safe and (best is None or matcher.ratio() > best[0]):
                best = (matcher.ratio(), start, end, ops)
        if best: candidates.append(best)
    candidates.sort(reverse=True, key=lambda item: item[0])
    if not candidates or (len(candidates) > 1 and candidates[0][0]-candidates[1][0] < .05): return None
    return candidates[0]


def apply_narration(final, youtube, whisper, text):
    # Independent YouTube AND actual-audio Whisper evidence is mandatory.
    streams = [words(blocks) for blocks in (final, youtube, whisper)]
    indices = [narration_index(tokens) for tokens, _ in streams]
    sentence_ends = [{i+1 for i, (cue, _, end, _) in enumerate(positions)
                      if re.match(r'[.!?](?:\s|$)', blocks[cue]['text'][end:])}
                     for (_, positions), blocks in zip(streams, (final, youtube, whisper))]
    cursors = [0, 0, 0]
    edits, accepted = {}, 0
    for number, paragraph in enumerate(narration_paragraphs(text), 1):
        reference, positions = words([{'text': paragraph}])
        matches = [narration_match(tokens, index, reference, cursor, endings)
                   for (tokens, _), index, cursor, endings in zip(streams, indices, cursors, sentence_ends)]
        if any(match is None for match in matches): continue
        score, start, end, ops = matches[0]
        # Base correction does not move cues; require evidence in the same YouTube cue range.
        fp, yp = streams[0][1], streams[1][1]
        ys, ye = matches[1][1:3]
        if (fp[start][0], fp[end-1][0]) != (yp[ys][0], yp[ye-1][0]): continue
        cursors = [match[2] for match in matches]
        accepted += 1
        log(f'REFERENCE MATCH paragraph={number} confidence={min(m[0] for m in matches):.3f} cues={fp[start][0]+1}-{fp[end-1][0]+1}')
        changes = []
        for tag, i1, i2, j1, j2 in ops:
            if tag == 'replace': changes.append((i1, i2, j1, j2))
            elif tag == 'equal':
                for k in range(i2-i1):
                    # Restore reference capitalization/spelling surfaces without adding prose.
                    changes.append((i1+k, i1+k+1, j1+k, j1+k+1))
        count = 0
        for i1, i2, j1, j2 in changes:
            first, last = fp[start+i1], fp[start+i2-1]
            if first[0] != last[0]: continue
            replacement = paragraph[positions[j1][1]:positions[j2-1][2]]
            current = final[first[0]]['text'][first[1]:last[2]]
            if current != replacement:
                edits.setdefault(first[0], []).append((first[1], last[2], replacement))
                count += 1
        if count: log(f'REFERENCE CORRECTION paragraph={number} spans={count}')
    if not accepted: log('REFERENCE LOW CONFIDENCE - IGNORED no strongly supported paragraph')
    for cue, replacements in edits.items():
        for start, end, replacement in sorted(replacements, reverse=True):
            final[cue]['text'] = final[cue]['text'][:start] + replacement + final[cue]['text'][end:]
    return final


def process(subs):
    if final_status(subs) == 'DONE': return 'FINAL ALREADY EXISTS'
    if not all((subs/n).is_file() and (subs/n).stat().st_size for n in SOURCES): return 'FINAL WAITING'
    log(f'FINAL GENERATING {subs.parent.name}')
    raw = {n:(subs/n).read_bytes() for n in SOURCES}
    parsed = {n:parse_srt(data.decode('utf-8-sig'), allow_legacy=True) for n, data in raw.items()}
    final, count = generate(parsed['youtube.srt'], parsed['en.srt'], parsed['hybrid.srt'])
    reference = narration_reference(subs.parent)
    if reference is not None:
        final = apply_narration(final, parsed['youtube.srt'], parsed['en.srt'], reference[2])
    for block in final:
        block['text'] = remove_fillers(block['text'])
    fd, temp = tempfile.mkstemp(prefix='.final.', suffix='.tmp', dir=subs)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(serialize(final)); f.flush(); os.fsync(f.fileno())
    validate(parse_srt(Path(temp).read_text()), parsed['youtube.srt'])
    if any((subs/n).read_bytes() != data for n, data in raw.items()):
        raise ValueError('Source changed during generation; retry on next scan')
    if reference is not None and reference[0].read_bytes() != reference[1]:
        raise ValueError('Narration reference changed during generation; retry on next scan')
    if (subs/'final.srt').exists():
        # Preserve any invalid previous output before its atomic replacement.
        backup = subs/('final.srt.backup_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        shutil.copy2(subs/'final.srt', backup)
    os.replace(temp, subs/'final.srt')
    return f'FINAL SUCCESS blocks={len(final)} corrections={count}'

def trigger_metadata(project, status):
    """Only validated final output can start the independent metadata generator."""
    if not (status.startswith('FINAL SUCCESS') or status == 'FINAL ALREADY EXISTS'):
        return
    try:
        subprocess.run([sys.executable, '-B', str(BASE / 'tools/metadata.py'),
                        '--project', str(project.absolute()), '--run'],
                       check=True, timeout=90)
    except Exception as exc:
        log(f'METADATA FAILED project={project.name}: {type(exc).__name__}: {exc}')
        traceback.print_exc()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, help='Process only this project')
    args = parser.parse_args()
    (BASE/'logs').mkdir(exist_ok=True)
    with (BASE/'.final_subtitle.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        state_path = BASE/'logs/final_subtitle_state.json'
        try: previous = json.loads(state_path.read_text())
        except (OSError, ValueError): previous = {}
        if not isinstance(previous, dict): previous = {}
        projects = [args.project.expanduser()] if args.project else [p for root in (BASE/'prepared', BASE/'done') if root.exists() for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith('.')]
        for project in projects:
            # Cooperate briefly with directory moves, without changing old workers.
            with (BASE/'.project_paths.lock').open('a') as pathlock:
                try: fcntl.flock(pathlock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError: continue
                if not project.exists(): continue
                try: status = process(project/'subs')
                except Exception as exc:
                    status = f'FINAL FAILED {type(exc).__name__}: {exc}'
                    traceback.print_exc()
                    try:
                        if (project/'subs').exists(): (project/'subs/.final_failed').write_text(status+'\n')
                    except OSError: pass
                trigger_metadata(project, status)
                key = str(project)
                if previous.get(key) != status: log(f'{status} project={project.name}')
                previous[key] = status
        temp = state_path.with_suffix('.json.tmp'); temp.write_text(json.dumps(previous, indent=2)); os.replace(temp, state_path)

if __name__ == '__main__': main()
