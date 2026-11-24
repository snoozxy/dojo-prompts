#!/usr/bin/env python3
"""
clean_subs.py — strip formatting noise from SRT/ASS subtitle files.

Removes: ASS override tags, HTML tags, ruby/furigana annotations, music symbols,
and other display-only garbage before passing to content2srs.
ASS/SSA input is converted to clean SRT output.

Usage:
    python3 clean_subs.py input.srt [-o output.srt]
    python3 clean_subs.py input.ass [-o output.srt]
    python3 clean_subs.py synced_subs/*.srt --inplace
    python3 clean_subs.py downloaded/*.ass --inplace   # writes .srt alongside each .ass

Exit codes:
    0  All files processed successfully
    1  One or more files had errors
"""

import argparse
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ── Text cleaning ─────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Strip formatting noise from a single subtitle line."""
    # ASS override tags {\...} — handles \t(), \pos(), colors, animations, etc.
    text = re.sub(r'\{[^}]*\}', '', text)

    # HTML tags <i>, <b>, <u>, <font color="...">, </i>, etc.
    text = re.sub(r'<[^>]+>', '', text)

    # Ruby/furigana: ｜word《reading》 → word  (standard vertical-text ruby)
    text = re.sub(r'｜([^《\s]+)《[^》]*》', r'\1', text)
    # word《reading》 without leading ｜ → word (short reading = furigana)
    text = re.sub(r'([^\s《])《[^》]{1,10}》', r'\1', text)

    # Inner-monologue / voice-over brackets: 《text》 → text
    # Any remaining 《 》 after the furigana pass are display-only bracket markers.
    text = text.replace('《', '').replace('》', '')

    # Voice-over speaker label after bracket strip: 翼：text → text
    # Pattern: short word (≤6 chars) + fullwidth colon at line start.
    text = re.sub(r'^[^\s：]{1,6}：', '', text)

    # Parens cleanup: three related rules applied together in a loop, since
    # resolving one can expose another (e.g. ((ガサッガサッ…(葉の音))) has a
    # single-paren sound tag nested one level deeper than the double-paren
    # dialogue wrapper — the tag only becomes matchable after nothing else
    # changes it, and the outer double-paren only becomes a clean pair once
    # the inner tag is gone).
    for _ in range(4):
        before = text

        # Kana-only furigana glued directly after a kanji word, no space:
        # 漢字（かんじ）/ 漢字(かんじ) → 漢字. Requires the char right before
        # the paren to be kanji so this can't misfire on real dialogue that
        # happens to be all-kana (common in casual speech) — furigana is
        # specifically a reading glued onto a preceding kanji token, not a
        # standalone kana parenthetical. Both paren widths appear in the
        # wild since some jimaku sources render furigana as plain text
        # instead of ASS ruby tags.
        text = re.sub(r'(?<=[一-龯々ヶ])[（(]([ぁ-ゟァ-ヿー]+)[）)]', '', text)

        # Double-paren wrapping marks real spoken dialogue (e.g. a flashback
        # or nested scene), not a tag — ((そう思う)) is actual speech, so
        # unwrap it to its content rather than deleting it.
        text = re.sub(r'[（(]{2}([^（()）]*)[）)]{2}', r'\1', text)

        # Character-name / speaker / sound-effect labels: （美波）text,
        # text（美波）, text（ドアの音）text → text. Short bracketed tags
        # (≤16 chars, no internal punctuation — long enough for a compound
        # Western-style name like 澤村･スペンサー･英梨々) anywhere in the
        # line — jimaku sources glue these onto the start, middle, or end
        # of a dialogue cue, not just the start, and use both half-width
        # and full-width parens. This is single-paren only (double-paren is
        # real dialogue, handled above); by the time this rule sees an
        # inner tag left over from an unresolved triple-nesting case, it
        # strips just that tag and leaves the surrounding dialogue for the
        # next loop iteration.
        text = re.sub(r'[（(][^）()]{1,16}[）)]', '', text)

        if text == before:
            break

    # Music/sound symbols
    text = re.sub(r'[♪♩♫♬]', '', text)

    # Continuation and display arrows — U+2192 family AND U+27A1 ➡ and kin
    text = re.sub(r'[→←↑↓↗↘↙↖▶▷◀◁➡➠➟➤➥➨➩➪➫➬➭➮➯➱]', '', text)

    # Miscellaneous display symbols (fansub speaker indicators, etc.)
    text = re.sub(r'[⚟⚠⚡★☆■□●○◆◇]', '', text)

    # Narration / voice-over delimiter brackets. jimaku wraps offscreen lines in
    # ＜…＞ or ｟…｠ blocks that often span several cues, so only the stray open/close
    # bracket leaks into a line (the leading "＜name：" speaker label is removed by
    # the rule above, but the closing bracket on the block's last cue survives).
    # These are never spoken text. 「」『』（） are left intact — normal quotation.
    text = re.sub(r'[＜＞〈〉｟｠]', '', text)

    # Broad emoji strip — Unicode emoji / pictograph ranges
    text = re.sub(
        r'[\U0001F300-\U0001F9FF'   # Misc symbols, emoticons, transport, etc.
        r'\U00002600-\U000027BF'    # Misc symbols, dingbats (excluding arrows kept above)
        r'\U0001FA00-\U0001FAFF]',  # Symbols and pictographs extended-A
        '', text,
    )

    # Normalize whitespace
    text = re.sub(r'[ \t　]+', ' ', text).strip()

    return text


def is_parenthetical_only(line: str) -> bool:
    """True if the entire line is a parenthetical sound/stage direction."""
    return bool(re.fullmatch(r'[（(][^）)]+[）)]', line.strip()))


def has_real_text(line: str) -> bool:
    """True if the line contains at least one Japanese or Latin letter."""
    return bool(re.search(r'[ぁ-ゟァ-ヿ一-鿿a-zA-Za-zA-Z]', line))


def split_at_monologue_end(start_ts: str, end_ts: str, lines: list[str]) -> list[tuple]:
    """
    Split a cue when a line closing inner monologue (ending 》) is followed by
    more lines on the same cue — those following lines are a different speaker.
    Both halves keep the same timestamps; content2srs handles card boundaries.
    Only runs on raw (pre-clean) lines so the 》 is still visible.
    """
    if len(lines) <= 1:
        return [(start_ts, end_ts, lines)]

    groups: list[list[str]] = []
    current: list[str] = []
    for i, line in enumerate(lines):
        current.append(line)
        if line.rstrip().endswith('》') and i < len(lines) - 1:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    if len(groups) <= 1:
        return [(start_ts, end_ts, lines)]
    return [(start_ts, end_ts, g) for g in groups]


# ── SRT parser / writer ───────────────────────────────────────────────────────

def parse_srt(raw: str) -> list[tuple[str, str, list[str]]]:
    """Parse SRT text into (start_ts, end_ts, content_lines) triples."""
    raw = raw.lstrip('﻿')
    cues = []
    for block in re.split(r'\r?\n\r?\n', raw.strip()):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        # Find the --> timing line (skip the sequence number)
        timing_idx = next((i for i, ln in enumerate(lines) if '-->' in ln), None)
        if timing_idx is None:
            continue
        m = re.match(
            r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})',
            lines[timing_idx],
        )
        if not m:
            continue
        # Normalise decimal point to comma (some SRTs use periods)
        start_ts = m.group(1).replace('.', ',')
        end_ts = m.group(2).replace('.', ',')
        content = lines[timing_idx + 1:]
        cues.append((start_ts, end_ts, content))
    return cues


def write_srt(cues: list[tuple[str, str, list[str]]], dst: Path):
    blocks = []
    for i, (start_ts, end_ts, lines) in enumerate(cues, 1):
        blocks.append(f"{i}\n{start_ts} --> {end_ts}\n" + "\n".join(lines))
    dst.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def clean_srt(src: Path, dst: Path) -> bool:
    try:
        raw = src.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        print(f"ERROR reading {src}: {e}", file=sys.stderr)
        return False

    cues = parse_srt(raw)
    cleaned = []
    for start_ts, end_ts, lines in cues:
        for s_ts, e_ts, grp in split_at_monologue_end(start_ts, end_ts, lines):
            new_lines = [clean_text(ln) for ln in grp]
            new_lines = [ln for ln in new_lines if ln and has_real_text(ln) and not is_parenthetical_only(ln)]
            if new_lines:
                cleaned.append((s_ts, e_ts, new_lines))

    write_srt(cleaned, dst)
    dropped = len(cues) - len(cleaned)
    note = f" ({dropped} empty/noise cues dropped)" if dropped else ""
    print(f"{src.name} → {dst.name}: {len(cues)} → {len(cleaned)} cues{note}", file=sys.stderr)
    return True


# ── ASS/SSA parser ────────────────────────────────────────────────────────────

def ass_ts_to_srt(ts: str) -> str:
    """Convert ASS timestamp H:MM:SS.cc (centiseconds) to SRT HH:MM:SS,mmm."""
    m = re.match(r'(\d+):(\d{2}):(\d{2})\.(\d{2})', ts.strip())
    if not m:
        return "00:00:00,000"
    h, mi, s, cs = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return f"{h:02d}:{mi:02d}:{s:02d},{cs * 10:03d}"


def parse_ass(raw: str) -> list[tuple[str, str, list[str]]]:
    """Parse ASS/SSA text into (start_ts, end_ts, content_lines) triples."""
    raw = raw.lstrip('﻿')

    events_m = re.search(r'\[Events\]', raw)
    if not events_m:
        return []

    events_section = raw[events_m.start():]
    lines = events_section.splitlines()

    # Determine field order from the Format: line
    format_fields: dict[str, int] = {}
    for ln in lines:
        if ln.startswith('Format:'):
            fields = [f.strip() for f in ln[len('Format:'):].split(',')]
            format_fields = {name: i for i, name in enumerate(fields)}
            break

    if not format_fields:
        return []

    start_idx = format_fields.get('Start', 1)
    end_idx = format_fields.get('End', 2)
    text_idx = format_fields.get('Text', 9)

    cues = []
    for ln in lines:
        if not ln.startswith('Dialogue:'):
            continue
        # Split on commas up to text_idx; text field may contain commas
        parts = ln[len('Dialogue:'):].split(',', text_idx)
        if len(parts) <= text_idx:
            continue

        start_ts = ass_ts_to_srt(parts[start_idx])
        end_ts = ass_ts_to_srt(parts[end_idx])
        raw_text = parts[text_idx]

        # ASS soft/hard line breaks and hard spaces
        raw_text = raw_text.replace(r'\N', '\n').replace(r'\n', '\n').replace(r'\h', ' ')

        content_lines = raw_text.splitlines()
        cues.append((start_ts, end_ts, content_lines))

    return cues


def clean_ass(src: Path, dst: Path) -> bool:
    # Try common Japanese encodings in order
    raw = None
    for enc in ('utf-8-sig', 'shift_jis', 'cp932', 'latin-1'):
        try:
            raw = src.read_text(encoding=enc, errors='strict')
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if raw is None:
        raw = src.read_text(encoding='utf-8', errors='replace')

    cues = parse_ass(raw)
    if not cues:
        print(f"WARNING: {src.name}: no Dialogue lines found — is this a valid ASS file?", file=sys.stderr)
        return False

    cleaned = []
    for start_ts, end_ts, lines in cues:
        for s_ts, e_ts, grp in split_at_monologue_end(start_ts, end_ts, lines):
            new_lines = [clean_text(ln) for ln in grp]
            new_lines = [ln for ln in new_lines if ln and has_real_text(ln) and not is_parenthetical_only(ln)]
            if new_lines:
                cleaned.append((s_ts, e_ts, new_lines))

    write_srt(cleaned, dst)
    dropped = len(cues) - len(cleaned)
    note = f" ({dropped} empty/noise cues dropped)" if dropped else ""
    print(f"{src.name} → {dst.name}: {len(cues)} → {len(cleaned)} cues{note}", file=sys.stderr)
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────

def resolve_dst(src: Path, out_arg: str | None, inplace: bool) -> Path:
    if out_arg:
        return Path(out_arg)
    suffix = src.suffix.lower()
    if inplace:
        # ASS → write .srt alongside; SRT → overwrite in place
        return src.with_suffix('.srt') if suffix in ('.ass', '.ssa') else src
    # Default: add .cleaned stem for SRT, swap extension for ASS
    if suffix in ('.ass', '.ssa'):
        return src.with_suffix('.srt')
    return src.with_stem(src.stem + '.cleaned')


def main():
    parser = argparse.ArgumentParser(
        description="Strip formatting noise from SRT/ASS subtitle files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('inputs', nargs='+', help='SRT or ASS files to clean')
    parser.add_argument('-o', '--out', help='Output path (single input file only)')
    parser.add_argument(
        '--inplace', action='store_true',
        help='Overwrite SRT files in place; write .srt alongside ASS files',
    )
    args = parser.parse_args()

    if args.out and len(args.inputs) > 1:
        parser.error("-o/--out can only be used with a single input file")
    if args.out and args.inplace:
        parser.error("--inplace and -o are mutually exclusive")

    errors = 0
    for inp in args.inputs:
        src = Path(inp)
        if not src.exists():
            print(f"ERROR: {inp}: file not found", file=sys.stderr)
            errors += 1
            continue

        dst = resolve_dst(src, args.out, args.inplace)
        suffix = src.suffix.lower()

        if suffix in ('.ass', '.ssa'):
            ok = clean_ass(src, dst)
        else:
            ok = clean_srt(src, dst)

        if not ok:
            errors += 1

    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
