#!/usr/bin/env python3
"""Test multiple subtitle candidates against a video and pick the best sync.

Runs ffsubsync on all candidates in parallel, ranks by absolute offset, and
copies the best match to the output path.

With --fuse, after the best subtitle is written it chains the rest of the
jimaku->fused pipeline in one command: clean_subs.py (in place) -> transcribe.py
(the video, for real timing + speaker labels) -> fuse_subs.py (jimaku text onto
that timing) -> a fused transcript JSON ready for `content2srs build`. An existing
--transcript is reused, so transcription (the expensive part) never runs twice.

Usage:
    python3 sync_subs.py <video> <candidates_dir> -o <output.srt>
    python3 sync_subs.py <video> <candidates_dir> -o <output.srt> --episode 3
    python3 sync_subs.py <video> <candidates_dir> -o <output.srt> --dry-run
    # One-shot jimaku -> fused.json (sync + clean + transcribe + fuse):
    python3 sync_subs.py <video> <candidates_dir> -o ep01.srt --episode 1 \
        --fuse --provider soniox --fused-out fused/ep01.json
    # Re-fuse an already-synced SRT without re-running ffsubsync (candidates_dir omitted):
    python3 sync_subs.py <video> -o ep01.srt --no-sync \
        --provider soniox --fused-out fused/ep01.json

Options:
    --episode N      Filter candidates to those matching episode number N
    --group SUBSTR   Filter candidates to filenames containing SUBSTR (case-insensitive).
                      Use after episode 1 identifies the winning fansub group so later
                      episodes sync against just that group instead of re-testing all of
                      them. Falls back to the unfiltered (episode-filtered) list with a
                      warning if nothing matches.
    --threshold T    Max acceptable offset in seconds (default: 300)
    --workers N      Parallel ffsubsync processes (default: 4)
    --dry-run        Show ranked table without writing output file
    --fuse           After syncing, clean + transcribe + fuse into a transcript JSON
    --no-sync        Skip ffsubsync; fuse the existing SRT at -o (implies --fuse).
                     candidates_dir may be omitted.
    --provider P     STT provider for --fuse (elevenlabs|soniox); required unless a
                     transcript already exists at --transcript
    --transcript P   Transcript JSON path (--fuse). Reused if present; else written
                     here. Default: <out>.transcript.json
    --fused-out P    Fused transcript JSON output (--fuse). Default: <out>.fused.json
    --language L     Transcription language (--fuse; default: ja)
    --no-clean       Skip clean_subs.py before fusing (--fuse)

Exit codes:
    0  Best match written to --out (and fused JSON written, if --fuse)
    1  No candidate passed the threshold (or --dry-run), or a --fuse step failed

Environment:
    FFSUBSYNC_EXE          Override path to ffsubsync executable
    ELEVENLABS_API_KEY / SONIOX_API_KEY    Read by transcribe.py for --fuse
"""

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import json

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SUB_EXTENSIONS = (".srt", ".ass", ".ssa")

# Matches dual-language subtitle filenames like [CHS+JPN] or [JPN+CHT].
# These produce mixed-language Anki cards and should be skipped when
# JPN-only alternatives are available.
_DUAL_LANG_PAT = re.compile(
    r"(?:CHS|CHT|chi|zh)\s*[+&]\s*(?:JPN|jpn|ja)"
    r"|(?:JPN|jpn|ja)\s*[+&]\s*(?:CHS|CHT|chi|zh)",
    re.IGNORECASE,
)

_WIN_SCRIPTS = (
    r"%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0"
    r"\LocalCache\local-packages\Python313\Scripts\ffsubsync.exe"
)


def _find_ffsubsync():
    exe = shutil.which("ffsubsync")
    if exe:
        return exe
    win = os.path.expandvars(_WIN_SCRIPTS)
    if os.path.isfile(win):
        return win
    return None


def _detect_jp_reference_stream(video):
    """Return a ffsubsync --reference-stream value if JP audio is not the first stream.

    Dual-audio encodes (e.g. EMBER) often have English as stream 0 and Japanese
    as stream 1. ffsubsync defaults to the first audio stream, which produces
    wildly wrong offsets when syncing Japanese subtitles against English audio.
    This probes the video and returns e.g. "a:1" when JP is not stream 0.
    Returns None when JP is already first (or when detection fails).
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index:stream_tags=language",
                "-of", "json",
                video,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        ja_langs = {"jpn", "ja", "japanese"}
        for i, s in enumerate(streams):
            lang = s.get("tags", {}).get("language", "").lower()
            if lang in ja_langs:
                if i > 0:
                    return f"a:{i}"
                return None  # JP is already the first audio stream
    except Exception:
        pass
    return None


def _find_candidates(directory, episode=None, group=None):
    all_candidates = []
    for fname in sorted(os.listdir(directory)):
        if not fname.lower().endswith(SUB_EXTENSIONS):
            continue
        if episode is not None:
            # Strip bracketed tokens (CRC hashes like [8F22448B], group tags)
            # before matching so hash digits don't false-positive as episode numbers.
            fname_stripped = re.sub(r"\[.*?\]", "", fname)
            pats = [
                rf"[Ee][Pp]?0*{episode}(?!\d)",       # E01, EP1, Ep01
                rf" - 0*{episode}(?!\d)",               # "Show - 01 [hash]" fansub format
                rf"[-_]0*{episode}(?=[-_.\s\[]|$)",    # _01. or _01[ or -01
            ]
            if not any(re.search(p, fname_stripped) for p in pats):
                continue
        all_candidates.append(os.path.join(directory, fname))

    # Prefer JPN-only files over dual-language (CHS+JPN, CHT+JPN, etc.).
    # Dual-language ASS files contain both Chinese and Japanese lines and
    # produce mixed-language Anki cards.
    single_lang = [c for c in all_candidates if not _DUAL_LANG_PAT.search(os.path.basename(c))]
    if single_lang:
        skipped = len(all_candidates) - len(single_lang)
        if skipped:
            print(f"  Skipped {skipped} dual-language file(s) — JPN-only alternatives found")
        candidates = single_lang
    else:
        # Fall back to dual-language files if no JPN-only alternative exists.
        candidates = all_candidates

    if group:
        group_lower = group.lower()
        narrowed = [c for c in candidates if group_lower in os.path.basename(c).lower()]
        if narrowed:
            return narrowed
        print(f"  --group '{group}' matched nothing — falling back to all candidates")

    return candidates


def _try_sync(ffsubsync, video, candidate, tmp_dir, reference_stream=None):
    name = os.path.basename(candidate)
    out_path = os.path.join(tmp_dir, re.sub(r"[^\w.-]", "_", name) + ".synced.srt")
    try:
        cmd = [ffsubsync, video, "-i", candidate, "-o", out_path]
        if reference_stream:
            cmd += ["--reference-stream", reference_stream]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=300,
        )
        combined = result.stdout + result.stderr
        m = re.search(r"[Oo]ffset\s+seconds?[:\s]+(-?[\d.]+)", combined)
        if m:
            offset = float(m.group(1))
            return (candidate, offset, out_path if os.path.isfile(out_path) else None)
        return (candidate, None, None)
    except subprocess.TimeoutExpired:
        return (candidate, None, None)
    except Exception:
        return (candidate, None, None)


def _default_transcript_path(out):
    return os.path.splitext(out)[0] + ".transcript.json"


def _default_fused_path(out):
    return os.path.splitext(out)[0] + ".fused.json"


def _run_fuse_chain(args):
    """Clean the synced SRT, transcribe the video, and fuse them into one JSON.

    Each step shells out to the sibling script with the current interpreter, so
    behaviour matches running them by hand. Transcription is skipped when the
    transcript already exists (it is the slow/paid step). Raises
    subprocess.CalledProcessError if any step fails.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))

    def run(script, *script_args):
        path = os.path.join(script_dir, script)
        if not os.path.isfile(path):
            sys.exit(f"--fuse needs {script} next to sync_subs.py (not found: {path})")
        print(f"\n>>> {script} {' '.join(script_args)}")
        subprocess.run([sys.executable, path, *script_args], check=True)

    srt = args.out
    transcript = args.transcript or _default_transcript_path(args.out)
    fused = args.fused_out or _default_fused_path(args.out)

    # 1. Clean the synced SRT in place (strip ASS tags, furigana, music symbols).
    if not args.no_clean:
        run("clean_subs.py", srt, "--inplace")

    # 2. Transcribe for real per-character timing + speaker_id (reuse if present).
    if os.path.exists(transcript):
        print(f"\nReusing existing transcript: {transcript}")
    elif args.provider:
        run("transcribe.py", "--provider", args.provider,
            "--language", args.language, "-o", transcript, args.video)
    else:
        sys.exit("--fuse needs --provider to transcribe (no existing --transcript).")

    # 3. Fuse jimaku text onto the AI timing/speakers.
    fused_dir = os.path.dirname(os.path.abspath(fused))
    if fused_dir:
        os.makedirs(fused_dir, exist_ok=True)
    run("fuse_subs.py", transcript, srt, "-o", fused)
    print(f"\nFused transcript ready: {fused}")


def _fuse_or_exit(args):
    """Run the fuse chain, exiting cleanly (not with a traceback) on step failure."""
    try:
        _run_fuse_chain(args)
    except subprocess.CalledProcessError as e:
        sys.exit(f"\nFuse step failed (exit {e.returncode}). The synced SRT is saved at "
                 f"{args.out} — rerun the fuse step (e.g. with --no-sync) once fixed.")


def _label(offset, threshold):
    if offset is None:
        return "error"
    a = abs(offset)
    if a <= 2:
        return "perfect"
    if a <= 30:
        return "good"
    if a <= threshold:
        return "questionable"
    return "DISCARD"


def main():
    ap = argparse.ArgumentParser(
        description="Rank subtitle candidates by sync quality and copy the best match."
    )
    ap.add_argument("video", help="Video file to sync against")
    ap.add_argument("candidates_dir", nargs="?", default=None,
                    help="Directory of candidate subtitle files (omit when using --no-sync)")
    ap.add_argument("-o", "--out", required=True, help="Output path for best-synced subtitle (.srt)")
    ap.add_argument("--episode", type=int, default=None,
                    help="Filter candidates to those matching this episode number")
    ap.add_argument("--group", default=None,
                    help="Filter candidates to filenames containing this substring "
                         "(case-insensitive). Use once episode 1 has identified the "
                         "winning fansub group.")
    ap.add_argument("--threshold", type=float, default=300.0,
                    help="Max acceptable absolute offset in seconds (default: 300)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel ffsubsync workers (default: 4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print ranked table without writing output file")
    ap.add_argument("--reference-stream", default=None,
                    help="ffsubsync --reference-stream override (e.g. 'a:1' for second audio track). "
                         "Auto-detected from the video when not set.")
    ap.add_argument("--fuse", action="store_true",
                    help="After syncing, clean + transcribe + fuse into a transcript JSON.")
    ap.add_argument("--no-sync", action="store_true",
                    help="Skip ffsubsync; fuse an already-synced SRT at -o (implies --fuse).")
    ap.add_argument("--provider", choices=["elevenlabs", "soniox"], default=None,
                    help="STT provider for --fuse (required unless --transcript exists).")
    ap.add_argument("--transcript", default=None,
                    help="Transcript JSON for --fuse; reused if present. Default: <out>.transcript.json")
    ap.add_argument("--fused-out", default=None,
                    help="Fused transcript JSON output for --fuse. Default: <out>.fused.json")
    ap.add_argument("--language", default="ja", help="Transcription language for --fuse (default: ja).")
    ap.add_argument("--no-clean", action="store_true",
                    help="Skip clean_subs.py before fusing (--fuse).")
    args = ap.parse_args()

    # --no-sync implies --fuse: with sync skipped, fusing is the only work left.
    if args.no_sync:
        args.fuse = True

    # Fail fast: --fuse with no way to obtain a transcript.
    if args.fuse and not args.provider:
        transcript = args.transcript or _default_transcript_path(args.out)
        if not os.path.exists(transcript):
            sys.exit("--fuse requires --provider (to transcribe) or an existing "
                     f"--transcript JSON (looked for {transcript}).")

    # --no-sync: skip ffsubsync entirely and fuse the already-synced SRT at -o.
    if args.no_sync:
        if not os.path.isfile(args.out):
            sys.exit(f"--no-sync expects an already-synced SRT at -o, but {args.out} "
                     f"does not exist.")
        print(f"Skipping sync — fusing existing SRT: {args.out}")
        _fuse_or_exit(args)
        return

    if not args.candidates_dir:
        ap.error("candidates_dir is required unless --no-sync is set")

    ffsubsync = os.environ.get("FFSUBSYNC_EXE") or _find_ffsubsync()
    if not ffsubsync:
        sys.exit(
            "ffsubsync not found. Install with: pip install ffsubsync\n"
            "Or set FFSUBSYNC_EXE=/path/to/ffsubsync"
        )

    candidates = _find_candidates(args.candidates_dir, args.episode, args.group)
    if not candidates:
        ep_suffix = f" for episode {args.episode}" if args.episode else ""
        sys.exit(f"No subtitle candidates found{ep_suffix} in {args.candidates_dir}")

    reference_stream = args.reference_stream or _detect_jp_reference_stream(args.video)
    if reference_stream:
        print(f"Using reference stream: {reference_stream} (JP audio detected)")

    print(f"Testing {len(candidates)} candidate(s) against: {os.path.basename(args.video)}")

    results = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        n_workers = min(args.workers, len(candidates))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_try_sync, ffsubsync, args.video, c, tmp_dir, reference_stream): c
                for c in candidates
            }
            done = 0
            for future in concurrent.futures.as_completed(futures):
                done += 1
                candidate, offset, synced_path = future.result()
                results.append((candidate, offset, synced_path))
                label = _label(offset, args.threshold)
                offset_str = f"{offset:+.2f}s" if offset is not None else "N/A"
                print(f"  [{done}/{len(candidates)}] {os.path.basename(candidate):50s}  {offset_str:>9}  {label}")

        results.sort(key=lambda r: abs(r[1]) if r[1] is not None else float("inf"))

        print(f"\n{'Rank':<5} {'Candidate':<54} {'Offset':>9}  Status")
        print("-" * 80)
        for i, (candidate, offset, _) in enumerate(results, 1):
            offset_str = f"{offset:+.2f}s" if offset is not None else "N/A"
            label = _label(offset, args.threshold)
            print(f"  #{i:<3} {os.path.basename(candidate):<54} {offset_str:>9}  {label}")

        best = next(
            (r for r in results if r[1] is not None and abs(r[1]) <= args.threshold and r[2]),
            None,
        )

        if best is None:
            print(f"\nNo candidate passed the {args.threshold}s threshold.")
            sys.exit(1)

        candidate, offset, synced_path = best

        if args.dry_run:
            print(f"\nDry run — best would be: {os.path.basename(candidate)} ({offset:+.2f}s)")
            sys.exit(1)

        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        shutil.copy2(synced_path, args.out)

    print(f"\nBest: {os.path.basename(candidate)} ({offset:+.2f}s) → {args.out}")

    if args.fuse:
        _fuse_or_exit(args)


if __name__ == "__main__":
    main()
