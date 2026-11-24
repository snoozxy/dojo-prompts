#!/usr/bin/env python3
"""Fuse accurate subtitle text with AI-transcript timing & speaker labels.

Human-authored Japanese subtitles (jimaku.cc, Plex sidecars) have the *correct*
text — names, jargon, kanji the AI mishears — but only display-line timing and no
speaker info. AI transcripts (transcribe.py: ElevenLabs Scribe / Soniox) have the
opposite: real per-character timestamps and `speaker_id`, but worse text.

This script aligns the two and writes a canonical transcript JSON that takes the
BEST of each:

    text       <- subtitle file   (accurate spelling, names, punctuation)
    start/end  <- AI transcript   (real per-character timing, via alignment)
    speaker_id <- AI transcript   (real diarization)

The output is the same shape transcribe.py produces, so it is a drop-in for the
content2srs JSON path (`build -s fused.json`) and for srt_watch.py / srt_translate.py.
Feeding it to content2srs gives Anki cards that break on real sentence AND speaker
boundaries, with audio cut at real word timing instead of interpolated cue edges.

    { "language_code": "jpn", "text": "...",
      "words": [ {"text":"世","start":13.72,"end":13.86,"type":"word",
                  "speaker_id":"speaker_1"}, ... ] }

Alignment uses difflib.SequenceMatcher over the two character streams
(autojunk=False — common kana must not be treated as junk). Matched runs copy AI
timing verbatim; mismatched runs (the AI's errors) keep the subtitle text and
linearly interpolate timing across the AI window they overlap.

Usage:
    python3 scripts/fuse_subs.py transcript.json episode.srt -o fused.json
    python3 scripts/fuse_subs.py transcript.json episode.srt   # -> episode.fused.json
"""
import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
from collections import Counter

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Max seconds to spread a single interpolated (non-exact) character over. Mora are
# typically well under this; it only clamps pathological smears where a short run
# of subtitle text aligns against a long mistranscribed AI window.
MAX_INTERP_CHAR_S = 0.6

# Speech closer together than this (seconds) is treated as one continuous run for
# speaker smoothing. Diarization flips with no real pause are collapsed to the run's
# majority speaker; runs separated by a longer gap (genuine turn-taking) keep their
# own labels. 0 disables smoothing.
SPEAKER_SMOOTH_GAP_S = 0.3


# ── inputs ──────────────────────────────────────────────────────────────────

def build_ai_chars(words):
    """Explode AI word tokens into per-character timing/speaker records.

    Mirrors content2srs `load_chars`: only `type == "word"` tokens contribute,
    multi-character tokens are split into single chars with linearly interpolated
    timing. Whitespace inside a token is dropped so positions match the subtitle
    character stream (which also drops whitespace).
    """
    chars = []
    for w in words:
        if w.get("type") != "word":
            continue
        cps = [c for c in w.get("text", "") if not c.isspace()]
        if not cps:
            continue
        start = float(w.get("start", 0.0))
        end = float(w.get("end", start))
        spk = w.get("speaker_id") or "speaker_0"
        dur = max(end - start, 0.0)
        n = len(cps)
        for i, c in enumerate(cps):
            chars.append({
                "ch": c,
                "start": start + dur * i / n,
                "end": start + dur * (i + 1) / n,
                "spk": spk,
            })
    return chars


def smooth_speakers(ai_chars, gap_s):
    """Collapse diarization flips that occur within continuous speech.

    STT diarization can flip speaker mid-utterance with no pause — e.g. a name and
    its following particle land on different speakers — which then forces a card to
    split mid-phrase. Within each run of speech closer together than *gap_s*, assign
    the run's majority speaker (by char count). Runs separated by a real pause keep
    their own labels, so genuine turn-taking (which carries a pause) is preserved.

    Returns the number of characters whose speaker label was changed.
    """
    if gap_s <= 0 or not ai_chars:
        return 0
    changed = 0
    start = 0
    n = len(ai_chars)
    for i in range(1, n + 1):
        if i == n or ai_chars[i]["start"] - ai_chars[i - 1]["end"] >= gap_s:
            run = ai_chars[start:i]
            maj = Counter(c["spk"] for c in run).most_common(1)[0][0]
            for c in run:
                if c["spk"] != maj:
                    c["spk"] = maj
                    changed += 1
            start = i
    return changed


def parse_srt_chars(path):
    """Return the subtitle's dialogue as one whitespace-free character list.

    Cue indices and timestamp lines are discarded — AI timing replaces them — and
    display line breaks within a cue are dropped, since the segmentation engine
    re-derives sentence/line structure. Punctuation (。！？、) is kept: it drives
    sentence splitting downstream.
    """
    with open(path, encoding="utf-8-sig") as f:
        content = f.read()
    out = []
    for block in re.split(r"\n[ \t]*\n", content.strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        lines = [ln for ln in lines if "-->" not in ln]
        for ch in "".join(lines):
            if not ch.isspace():
                out.append(ch)
    return out


# ── fusion ──────────────────────────────────────────────────────────────────

def _norm_key(ch):
    """Comparison key for alignment: NFKC-fold + casefold so width/case variants
    match (fullwidth ＢＬ vs BL, ７ vs 7, ！ vs !). Output text keeps the original
    jimaku character — this only affects what difflib considers 'equal'."""
    return unicodedata.normalize("NFKC", ch).casefold()


def fuse(ai_chars, sub_chars):
    """Align subtitle chars (b) onto AI chars (a); return canonical word records.

    For each difflib opcode:
      equal   — copy AI timing/speaker char-for-char (exact).
      replace — subtitle text overlaps the AI window [i1, i2); interpolate timing
                across that window, speaker from its first AI char.
      insert  — subtitle has chars the AI missed; place them between the last
                emitted char and the next matched AI char (zero-width at the ends).
      delete  — AI heard chars the subtitle omits; drop them (subtitle is canon).

    Inserts are anchored to the *last emitted char* (`last_end`), not to the
    surrounding AI indices: a 'delete' gap (e.g. an untranscribed OP/ED, or singing)
    can leave ai_chars[i1-1] and ai_chars[-1] far in time from where the subtitle
    text actually is, which would otherwise stretch a trailing insert across the
    whole gap and produce a minutes-long card.
    """
    # Align on normalized keys (per-char, 1:1 with the originals) but emit the
    # original jimaku text. difflib works on any sequence of hashable elements.
    a = [_norm_key(c["ch"]) for c in ai_chars]
    b = [_norm_key(ch) for ch in sub_chars]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)

    out = []
    exact = 0
    # Running anchor: end time / speaker of the most recently emitted char.
    last_end = ai_chars[0]["start"] if ai_chars else 0.0
    last_spk = ai_chars[0]["spk"] if ai_chars else "speaker_0"

    def emit(ch, start, end, spk):
        nonlocal last_end, last_spk
        start = max(start, 0.0)
        end = max(end, start)
        out.append({"text": ch, "start": round(start, 3), "end": round(end, 3),
                    "type": "word", "speaker_id": spk})
        last_end, last_spk = end, spk

    def emit_interp(chars_b, start, end, spk):
        n = len(chars_b)
        if n == 0:
            return
        # Cap the span so a few subtitle chars replacing a long AI window (e.g. an
        # acronym or rare token the AI mistranscribed, leaving a wide unmatched run)
        # don't smear across many seconds and inflate the card. The excess AI time
        # is left as a gap, which content2srs splits on — keeping cards compact and
        # anchored to where the matched text resumed.
        span = min(max(end - start, 0.0), n * MAX_INTERP_CHAR_S)
        for i, ch in enumerate(chars_b):
            emit(ch, start + span * i / n, start + span * (i + 1) / n, spk)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                a_ch = ai_chars[i1 + k]
                emit(sub_chars[j1 + k], a_ch["start"], a_ch["end"], a_ch["spk"])
            exact += i2 - i1
        elif tag == "replace":
            seg = ai_chars[i1:i2]
            emit_interp(sub_chars[j1:j2], seg[0]["start"], seg[-1]["end"], seg[0]["spk"])
        elif tag == "insert":
            # Place between where we left off and the next matched AI char.
            if i1 < len(ai_chars):
                nxt_start, nxt_spk = ai_chars[i1]["start"], ai_chars[i1]["spk"]
            else:
                nxt_start, nxt_spk = last_end, last_spk
            emit_interp(sub_chars[j1:j2], last_end, max(nxt_start, last_end), nxt_spk)
        # tag == "delete": AI-only chars, intentionally dropped.

    return out, exact, sm.ratio()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Fuse subtitle text with AI-transcript timing & speakers.")
    ap.add_argument("transcript", help="AI transcript JSON from transcribe.py.")
    ap.add_argument("subtitles", help="Cleaned subtitle file (.srt; run clean_subs.py first).")
    ap.add_argument("-o", "--output",
                    help="Output JSON path (default: <subtitles stem>.fused.json).")
    ap.add_argument("--lang", default="jpn", help="language_code for output (default: jpn).")
    ap.add_argument("--min-ratio", type=float, default=0.5,
                    help="Warn if char-match ratio falls below this (default: 0.5).")
    ap.add_argument("--speaker-smooth-gap", type=float, default=SPEAKER_SMOOTH_GAP_S,
                    help="Collapse diarization speaker flips within speech closer than "
                         f"this many seconds (default: {SPEAKER_SMOOTH_GAP_S}; 0 disables).")
    args = ap.parse_args()

    for p in (args.transcript, args.subtitles):
        if not os.path.exists(p):
            sys.exit(f"File not found: {p}")

    with open(args.transcript, encoding="utf-8-sig") as f:
        transcript = json.load(f)
    ai_chars = build_ai_chars(transcript.get("words", []))
    if not ai_chars:
        sys.exit("Transcript has no usable word tokens — cannot supply timing.")
    smoothed = smooth_speakers(ai_chars, args.speaker_smooth_gap)

    sub_chars = parse_srt_chars(args.subtitles)
    if not sub_chars:
        sys.exit("Subtitle file has no dialogue text.")

    words, exact, ratio = fuse(ai_chars, sub_chars)
    if not words:
        sys.exit("Fusion produced no words — check that the two files are the same episode.")

    out_path = args.output or (os.path.splitext(args.subtitles)[0] + ".fused.json")
    if out_path.endswith(".json") is False:
        out_path += ".json"

    data = {
        "language_code": args.lang,
        "text": "".join(w["text"] for w in words),
        "words": words,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)

    exact_pct = 100.0 * exact / len(words)
    smooth_note = f", {smoothed} speaker flips smoothed" if smoothed else ""
    print(f"Wrote {out_path} "
          f"({len(words)} chars, {exact_pct:.1f}% exact AI timing, "
          f"{100.0 - exact_pct:.1f}% interpolated; align ratio {ratio:.2f}{smooth_note})")
    if ratio < args.min_ratio:
        print(f"WARNING: low alignment ratio ({ratio:.2f} < {args.min_ratio}). "
              f"The transcript and subtitle may be different episodes, or the "
              f"subtitle may need cleaning (clean_subs.py).", file=sys.stderr)


if __name__ == "__main__":
    main()
