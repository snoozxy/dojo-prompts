#!/usr/bin/env python3
"""Helper for the primed-summaries skill.

Drives a sliding-window summarization loop where the agent (in the harness)
acts as the AI for each window. Python handles SRT parsing, window state,
JSON validation, contiguity repair, and final SRT assembly — the agent only
reads window inputs and writes JSON outputs.

One episode subagent runs the whole pipeline against its own isolated work
dir (so several episodes can run in parallel without clobbering each other).
Select the work dir with the global `--work-dir <path>` flag (or the
`PRIMED_SUMMARIES_DIR` env var); it defaults to /tmp/primed-summaries.

Modes:
    make-workdir <input_path>      Print WORKDIR/OUTPUT/OUTPUT_ALT for an input.
    prepare <input_path>           Parse transcript JSON or SRT, init state.
    next-window [source_language]  Build window brief (premise + recent + window).
    accept <json_path>             Validate/repair JSON, advance cursor.
    fallback                       Force-accept the current window with placeholder
                                   summaries and log it to failed_windows.txt.
    finalize <output_srt_path>     Build final summary SRT from accepted chunks.

`prepare` accepts either a transcript JSON (preferred — split on punctuation,
speaker change, or long pauses) or an SRT (one block = one sentence). It also
clears stale state in the work dir, guarded by a `.primed-summaries-workdir`
ownership marker so it can never delete unrelated files. The episode subagent
writes premise.txt directly; this helper reads it.

State files in the work dir (default /tmp/primed-summaries/):
    .primed-summaries-workdir  Ownership marker (this dir is ours to clean).
    blocks.json     Parsed SRT blocks: [{index, timecode, text}, ...]
    state.json      {cursor, total, accepted: [{start, end, summary}, ...]}
    premise.txt     2-4 sentence premise written by the episode subagent.
    window_brief.txt   Per-window brief consumed by the episode subagent.
    window_output.json Per-window JSON written by the episode subagent.
    full_transcript.txt  Numbered transcript for premise + language detection.
    failed_windows.txt   Spans that fell back to placeholder summaries.
"""

import hashlib
import json
import os
import re
import sys
import time

WINDOW_SIZE = 40
CORE_SIZE = 25
RECENT_SUMMARIES = 3
LONG_INPUT_WINDOWS = 40  # est. windows above which we warn about episode length
SENTENCE_ENDERS = frozenset('。！？!?')
PAUSE_THRESHOLD = 1.0  # seconds — gap between words that forces a sentence break
PLACEHOLDER_SUMMARY = '[summary unavailable — review this span]'
FALLBACK_CHUNK_SIZE = 5  # sentences per chunk when forcing a fallback window
MARKER_NAME = '.primed-summaries-workdir'

DEFAULT_STATE_DIR = '/tmp/primed-summaries'

# Source-language code suffixes stripped from a stem (lowercased, extensible).
LANG_CODE_SUFFIXES = frozenset({
    'ja', 'jp', 'ko', 'zh', 'zh-hans', 'zh-hant', 'zh-cn', 'zh-tw', 'en', 'es',
    'pt', 'pt-br', 'fr', 'de', 'it', 'ru', 'ar', 'hi', 'th', 'vi', 'id', 'tr',
    'pl', 'nl', 'sv',
})

# Files the helper owns and may unlink during cleanup. Never rmtree the dir.
STATE_FILE_NAMES = (
    'blocks.json', 'state.json', 'premise.txt', 'window_brief.txt',
    'window_output.json', 'full_transcript.txt', 'failed_windows.txt',
)


def set_work_dir(path):
    """Rebind the module-level path globals to live under `path`."""
    global STATE_DIR, BLOCKS_PATH, STATE_PATH, PREMISE_PATH, BRIEF_PATH
    global FULL_TRANSCRIPT_PATH, FAILED_PATH, MARKER_PATH
    STATE_DIR = path
    BLOCKS_PATH = os.path.join(STATE_DIR, 'blocks.json')
    STATE_PATH = os.path.join(STATE_DIR, 'state.json')
    PREMISE_PATH = os.path.join(STATE_DIR, 'premise.txt')
    BRIEF_PATH = os.path.join(STATE_DIR, 'window_brief.txt')
    FULL_TRANSCRIPT_PATH = os.path.join(STATE_DIR, 'full_transcript.txt')
    FAILED_PATH = os.path.join(STATE_DIR, 'failed_windows.txt')
    MARKER_PATH = os.path.join(STATE_DIR, MARKER_NAME)


# Seed paths from the env var (or default); --work-dir may override in main().
set_work_dir(os.environ.get('PRIMED_SUMMARIES_DIR', DEFAULT_STATE_DIR))


def _fmt_srt_time(seconds):
    if seconds < 0:
        seconds = 0
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    ms = int(round((s - int(s)) * 1000))
    return f'{h:02d}:{int(m):02d}:{int(s):02d},{ms:03d}'


def _parse_srt_timecode(tc):
    """Parse 'HH:MM:SS,mmm --> HH:MM:SS,mmm' into (start_sec, end_sec)."""
    def to_sec(t):
        t = t.replace(',', '.')
        h, m, s = t.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    start, end = [p.strip() for p in tc.split(' --> ')]
    return to_sec(start), to_sec(end)


def parse_srt(srt_path):
    with open(srt_path, encoding='utf-8-sig') as f:
        content = f.read().replace('\r\n', '\n')
    blocks = []
    for raw in re.split(r'\n\n+', content.strip()):
        lines = raw.strip().split('\n')
        if len(lines) < 2:
            continue
        timecode = lines[1].strip()
        try:
            start_sec, end_sec = _parse_srt_timecode(timecode)
        except (ValueError, IndexError):
            continue
        blocks.append({
            'timecode': timecode,
            'start': start_sec,
            'end': end_sec,
            'text': ' '.join(ln.strip() for ln in lines[2:] if ln.strip()),
        })
    return blocks


def parse_scribe_json(json_path):
    """Parse a transcript JSON file into sentence-level blocks.

    Splits on sentence-ending punctuation, speaker changes, and pauses
    >= PAUSE_THRESHOLD seconds. Each block has {timecode, start, end, text}.
    """
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)

    words = [w for w in data.get('words', []) if w.get('type') == 'word']
    if not words:
        return []

    blocks = []
    cur_words = []
    cur_speaker = None
    prev_end = None

    def flush():
        if not cur_words:
            return
        text = ''.join(w['text'] for w in cur_words).strip()
        if not text:
            return
        start = cur_words[0]['start']
        end = cur_words[-1]['end']
        blocks.append({
            'timecode': f'{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}',
            'start': start,
            'end': end,
            'text': text,
        })

    for w in words:
        speaker = w.get('speaker_id')
        # Speaker change or long pause forces a break before this word
        if cur_words:
            speaker_changed = cur_speaker is not None and speaker != cur_speaker
            long_pause = prev_end is not None and (w['start'] - prev_end) >= PAUSE_THRESHOLD
            if speaker_changed or long_pause:
                flush()
                cur_words = []

        cur_words.append(w)
        cur_speaker = speaker
        prev_end = w['end']

        # Sentence-ending punctuation closes the sentence (punctuation stays with it)
        text = w['text']
        if text and text[-1] in SENTENCE_ENDERS:
            flush()
            cur_words = []
            cur_speaker = None

    flush()
    return blocks


def parse_input(path):
    """Parse either a transcript JSON or an SRT into a list of blocks."""
    lower = path.lower()
    if lower.endswith('.json'):
        return parse_scribe_json(path)
    return parse_srt(path)


# ── naming helpers ─────────────────────────────────────────────────────────────

def strip_lang_code(stem):
    """Strip a trailing source-language code from a stem, if it is a known code.

    `stem` is a basename with its final extension (.srt/.json) already removed.
    Only the final dotted segment is considered, and only stripped when it is in
    LANG_CODE_SUFFIXES (case-insensitive) — so non-language segments like
    `.raw`, `.act`, or `.ova` are left intact. At most one segment is removed.
    """
    head, dot, last = stem.rpartition('.')
    if dot and last.lower() in LANG_CODE_SUFFIXES:
        return head
    return stem


def _sanitize_stem(stem):
    """Lowercase a stem and map every non-[a-z0-9] char to '_'."""
    return re.sub(r'[^a-z0-9]', '_', stem.lower())


def cmd_make_workdir(input_path):
    """Print WORKDIR / OUTPUT / OUTPUT_ALT lines for an input path.

    Centralizes all name derivation so every caller produces identical paths.
    Values are printed raw (one KEY=VALUE per line); callers must read
    everything after the first '=' as the literal value and shell-quote it
    themselves when building commands.
    """
    abs_path = os.path.realpath(input_path)
    base = os.path.basename(abs_path)
    full_stem = base.rsplit('.', 1)[0] if '.' in base else base
    lang_stripped = strip_lang_code(full_stem)

    h = hashlib.sha1(abs_path.encode('utf-8')).hexdigest()[:8]
    episode_id = f'{_sanitize_stem(lang_stripped)}-{h}-{os.getpid()}-{time.monotonic_ns()}'
    # Root the episode dir under the configured base (STATE_DIR honors
    # PRIMED_SUMMARIES_DIR / --work-dir), defaulting to /tmp/primed-summaries.
    workdir = os.path.join(STATE_DIR, episode_id)

    input_dir = os.path.dirname(abs_path)
    output = os.path.join(input_dir, f'{lang_stripped}.summary.en.srt')
    output_alt = os.path.join(input_dir, f'{full_stem}.summary.en.srt')

    print(f'WORKDIR={workdir}')
    print(f'OUTPUT={output}')
    print(f'OUTPUT_ALT={output_alt}')


# ── work-dir validation & ownership-guarded cleanup ────────────────────────────

def _validate_work_dir():
    """Refuse to operate on dangerous work dirs (called before any cleanup)."""
    raw = STATE_DIR
    if not raw or not raw.strip():
        print('ERROR: --work-dir is empty', file=sys.stderr)
        sys.exit(2)
    real = os.path.realpath(raw)
    home = os.path.realpath(os.path.expanduser('~'))
    if real == os.path.sep or os.path.dirname(real) == real:
        print(f'ERROR: refusing to use filesystem root as work dir: {raw}', file=sys.stderr)
        sys.exit(2)
    if real == home:
        print(f'ERROR: refusing to use $HOME as work dir: {raw}', file=sys.stderr)
        sys.exit(2)
    # Strip trailing separators first: os.path.islink('/tmp/link/') is False
    # because the trailing slash resolves through the link.
    norm = raw.rstrip(os.sep) or os.sep
    if os.path.islink(norm):
        print(f'ERROR: refusing to use a symlinked work dir: {raw}', file=sys.stderr)
        sys.exit(2)


def _prepare_work_dir():
    """Create/clear the work dir, guarded by the ownership marker.

    - Missing or empty dir  -> create, write marker, proceed.
    - Marker present        -> owned; clear known state files, proceed.
    - Non-empty, no marker  -> legacy migration if it is the default path and
                               holds only known state files; else refuse.
    """
    _validate_work_dir()

    if not os.path.exists(STATE_DIR):
        os.makedirs(STATE_DIR, exist_ok=True)
        _write_marker()
        return

    entries = os.listdir(STATE_DIR)
    has_marker = MARKER_NAME in entries
    non_marker = [e for e in entries if e != MARKER_NAME]

    if not non_marker:
        _write_marker()
        return
    if has_marker:
        _clear_state_files()
        return

    # Non-empty, unmarked: only safe to adopt the legacy default dir when it
    # contains nothing but our own known state files.
    is_legacy_default = os.path.realpath(STATE_DIR) == os.path.realpath(DEFAULT_STATE_DIR)
    only_known = all(e in STATE_FILE_NAMES for e in non_marker)
    if is_legacy_default and only_known:
        _clear_state_files()
        _write_marker()
        return

    print(
        f'ERROR: {STATE_DIR} is not a primed-summaries work dir '
        f'(no {MARKER_NAME} marker and it contains other files). Refusing to touch it.',
        file=sys.stderr,
    )
    sys.exit(2)


def _write_marker():
    with open(MARKER_PATH, 'w', encoding='utf-8') as f:
        f.write('primed-summaries work dir — safe to clean.\n')


def _clear_state_files():
    for name in STATE_FILE_NAMES:
        p = os.path.join(STATE_DIR, name)
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def load_state():
    with open(STATE_PATH, encoding='utf-8') as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_blocks():
    with open(BLOCKS_PATH, encoding='utf-8') as f:
        return json.load(f)


# ── prepare ──────────────────────────────────────────────────────────────────

def cmd_prepare(input_path):
    # Parse first so we never disturb the work dir for an unusable input.
    blocks = parse_input(input_path)
    if not blocks:
        print(f'ERROR: no sentences found in {input_path}', file=sys.stderr)
        sys.exit(1)
    _prepare_work_dir()
    with open(BLOCKS_PATH, 'w', encoding='utf-8') as f:
        json.dump(blocks, f, ensure_ascii=False)
    # Also write a plain numbered transcript for the premise / language-detection step
    with open(FULL_TRANSCRIPT_PATH, 'w', encoding='utf-8') as f:
        for i, b in enumerate(blocks, 1):
            f.write(f'{i}. {b["text"]}\n')
    state = {'cursor': 0, 'total': len(blocks), 'accepted': []}
    save_state(state)
    est_windows = max(1, (len(blocks) + CORE_SIZE - 1) // CORE_SIZE)
    print(f'Prepared {len(blocks)} sentences. Estimated ~{est_windows} windows.')
    print(f'Full transcript written to {FULL_TRANSCRIPT_PATH}')
    if est_windows > LONG_INPUT_WINDOWS:
        print(
            f'WARNING: ~{est_windows} windows is large for a single episode '
            f'subagent context (>{LONG_INPUT_WINDOWS}). Consider splitting inputs '
            f'longer than ~60-90 min into parts.'
        )


# ── next-window ──────────────────────────────────────────────────────────────

def cmd_next_window(source_language='the source language'):
    state = load_state()
    blocks = load_blocks()
    cursor = state['cursor']
    total = state['total']
    if cursor >= total:
        print('DONE')
        return
    window_end = min(cursor + WINDOW_SIZE, total)
    is_final = (window_end == total)
    window = blocks[cursor:window_end]

    if os.path.exists(PREMISE_PATH):
        with open(PREMISE_PATH, encoding='utf-8') as f:
            premise = f.read().strip()
    else:
        premise = '(no premise available)'

    recent = state['accepted'][-RECENT_SUMMARIES:]
    if recent:
        recent_block = '\n'.join(
            f'- (sentences {c["start"]}-{c["end"]}) {c["summary"]}' for c in recent
        )
    else:
        recent_block = '(none — this is the first window)'

    numbered = '\n'.join(f'{i + 1}. {b["text"]}' for i, b in enumerate(window))
    batch_count = len(window)

    brief = f'''You are a content summarizer for {source_language} audio content.
You will receive a numbered list of {source_language} sentences from a video transcript.
Group them into coherent topical chunks and provide a concise English summary for each chunk.

=== CONTENT PREMISE (full-show context) ===
{premise}

=== RECENTLY SUMMARIZED CHUNKS (just before this window) ===
{recent_block}

=== SENTENCES TO SUMMARIZE (this window only) ===
{numbered}

Rules:
- Find natural breakpoints where the topic, scene, or speaker changes
- Each chunk should contain 3-15 sentences
- Write a concise English summary (1-3 sentences) capturing the key meaning
- Use the premise and recent summaries to keep names, ongoing topics, and tone consistent
- The summary should help a listener understand what they are about to hear
- Every sentence in this window must belong to exactly one chunk (no gaps, no overlaps)

Return ONLY valid JSON (no markdown, no commentary):
[
  {{"start": 1, "end": 5, "summary": "The speaker introduces themselves."}},
  {{"start": 6, "end": 12, "summary": "Discussion of the main topic."}}
]

start/end are 1-based sentence numbers within THIS window only (1..{batch_count}).
The first chunk must start at 1; the last chunk must end at {batch_count}.
Chunks must be contiguous (no gaps, no overlaps).
'''
    with open(BRIEF_PATH, 'w', encoding='utf-8') as f:
        f.write(brief)
    flag = 'FINAL' if is_final else 'PARTIAL'
    print(f'{flag} cursor={cursor} window_size={batch_count} core_size={CORE_SIZE} brief={BRIEF_PATH}')


# ── accept ──────────────────────────────────────────────────────────────────

def _parse_and_repair(chunks, batch_count):
    if not isinstance(chunks, list) or not chunks:
        raise ValueError('Response is not a non-empty JSON array')
    for c in chunks:
        if not isinstance(c, dict):
            raise ValueError(f'Chunk is not a dict: {c}')
        for k in ('start', 'end', 'summary'):
            if k not in c:
                raise ValueError(f"Missing key '{k}' in chunk: {c}")
        if not isinstance(c['start'], int) or not isinstance(c['end'], int):
            raise ValueError(f'start/end must be integers: {c}')
        if c['start'] > c['end']:
            raise ValueError(f'start > end: {c}')
        if not c['summary'] or not str(c['summary']).strip():
            raise ValueError(f'Empty summary: {c}')

    chunks.sort(key=lambda c: c['start'])
    if chunks[0]['start'] != 1:
        raise ValueError(f"First chunk starts at {chunks[0]['start']}, expected 1")
    for i in range(1, len(chunks)):
        if chunks[i]['start'] <= chunks[i - 1]['end']:
            raise ValueError(
                f"Overlap: chunk ending at {chunks[i-1]['end']} and "
                f"chunk starting at {chunks[i]['start']}"
            )
        if chunks[i]['start'] > chunks[i - 1]['end'] + 1:
            chunks[i - 1]['end'] = chunks[i]['start'] - 1
    if chunks[-1]['end'] > batch_count:
        raise ValueError(f"Last chunk ends at {chunks[-1]['end']}, exceeds {batch_count}")
    if chunks[-1]['end'] < batch_count:
        chunks[-1]['end'] = batch_count
    return chunks


def cmd_accept(json_path):
    state = load_state()
    cursor = state['cursor']
    total = state['total']
    window_end = min(cursor + WINDOW_SIZE, total)
    is_final = (window_end == total)
    batch_count = window_end - cursor

    with open(json_path, encoding='utf-8') as f:
        raw = f.read().strip()
    if raw.startswith('```'):
        lines = raw.split('\n')
        if lines[-1].strip() == '```':
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        raw = '\n'.join(lines).strip()
    chunks = json.loads(raw)
    chunks = _parse_and_repair(chunks, batch_count)
    accepted = _commit_window(state, chunks, cursor, total, is_final)
    print(f"Accepted {len(accepted)} chunks. cursor={state['cursor']}/{total}")


def _commit_window(state, chunks, cursor, total, is_final):
    """Remap window-local chunks to global indices, apply the core-region
    filter (unless final), append to state, and advance the cursor. Returns the
    list of accepted chunks. Mutates and saves `state`.
    """
    # Remap window-local 1-based to global 1-based
    for c in chunks:
        c['start'] += cursor
        c['end'] += cursor

    if is_final:
        accepted = chunks
    else:
        core_limit = cursor + CORE_SIZE
        accepted = []
        for c in chunks:
            if c['end'] <= core_limit:
                accepted.append(c)
            else:
                break
        if not accepted:
            # One giant chunk; force-accept first to avoid an infinite loop
            accepted.append(chunks[0])

    state['accepted'].extend(
        {'start': c['start'], 'end': c['end'], 'summary': str(c['summary']).strip()}
        for c in accepted
    )
    state['cursor'] = accepted[-1]['end']
    save_state(state)
    return accepted


# ── fallback ──────────────────────────────────────────────────────────────────

def cmd_fallback():
    """Force-accept the current window with uniform placeholder chunks.

    Used after repeated accept failures: writes FALLBACK_CHUNK_SIZE-sentence
    chunks with a non-empty placeholder summary (so accept-style validation
    passes), commits them, and logs the span to failed_windows.txt for a manual
    revisit.
    """
    state = load_state()
    cursor = state['cursor']
    total = state['total']
    if cursor >= total:
        print('DONE — nothing to fall back on.')
        return
    window_end = min(cursor + WINDOW_SIZE, total)
    is_final = (window_end == total)
    batch_count = window_end - cursor

    chunks = []
    pos = 1
    while pos <= batch_count:
        end = min(pos + FALLBACK_CHUNK_SIZE - 1, batch_count)
        chunks.append({'start': pos, 'end': end, 'summary': PLACEHOLDER_SUMMARY})
        pos = end + 1

    accepted = _commit_window(state, chunks, cursor, total, is_final)
    # Log the sentences that ACTUALLY got placeholders. On a non-final window the
    # core-region filter commits only a prefix, so the attempted window size
    # (batch_count) would overstate the placeholder span.
    placeholder_start = accepted[0]['start']
    placeholder_end = accepted[-1]['end']
    with open(FAILED_PATH, 'a', encoding='utf-8') as f:
        f.write(
            f'sentences {placeholder_start}-{placeholder_end} '
            f'(attempted window {cursor + 1}-{window_end})\n'
        )
    print(
        f'Fell back on placeholder summaries for sentences '
        f'{placeholder_start}-{placeholder_end}. '
        f"cursor={state['cursor']}/{total}. Logged to {FAILED_PATH}"
    )


# ── finalize ─────────────────────────────────────────────────────────────────

def cmd_finalize(output_path):
    state = load_state()
    blocks = load_blocks()
    if state['cursor'] < state['total']:
        print(f"ERROR: not done — cursor={state['cursor']}/{state['total']}", file=sys.stderr)
        sys.exit(1)

    out_lines = []
    for seq, c in enumerate(state['accepted'], 1):
        first = blocks[c['start'] - 1]
        last = blocks[c['end'] - 1]
        start_ts = first['timecode'].split(' --> ')[0].strip()
        end_ts = last['timecode'].split(' --> ')[1].strip()
        out_lines.append(str(seq))
        out_lines.append(f'{start_ts} --> {end_ts}')
        out_lines.append(c['summary'])
        out_lines.append('')

    content = '\n'.join(out_lines)
    if not content.endswith('\n'):
        content += '\n'
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'Wrote {output_path} with {len(state["accepted"])} chunks.')


# ── main ─────────────────────────────────────────────────────────────────────

def _extract_work_dir(argv):
    """Strip a `--work-dir <val>` / `--work-dir=<val>` flag from argv (in place).

    Returns the remaining argv. Applies the work dir via set_work_dir() if the
    flag is present. A bare `--work-dir` with no value, or `--work-dir=`, is a
    hard error (never falls through to the default).
    """
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--work-dir':
            if i + 1 >= len(argv):
                print('ERROR: --work-dir requires a value', file=sys.stderr)
                sys.exit(2)
            val = argv[i + 1]
            if not val.strip():
                print('ERROR: --work-dir value is empty', file=sys.stderr)
                sys.exit(2)
            set_work_dir(val)
            i += 2
            continue
        if a.startswith('--work-dir='):
            val = a[len('--work-dir='):]
            if not val.strip():
                print('ERROR: --work-dir value is empty', file=sys.stderr)
                sys.exit(2)
            set_work_dir(val)
            i += 1
            continue
        out.append(a)
        i += 1
    return out


if __name__ == '__main__':
    argv = _extract_work_dir(sys.argv[1:])
    if not argv:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    mode = argv[0]
    if mode == 'make-workdir':
        cmd_make_workdir(argv[1])
    elif mode == 'prepare':
        cmd_prepare(argv[1])
    elif mode == 'next-window':
        lang = argv[1] if len(argv) > 1 else 'the source language'
        cmd_next_window(lang)
    elif mode == 'accept':
        cmd_accept(argv[1])
    elif mode == 'fallback':
        cmd_fallback()
    elif mode == 'finalize':
        cmd_finalize(argv[1])
    else:
        print(f'Unknown mode: {mode}', file=sys.stderr)
        sys.exit(2)
