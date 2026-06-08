#!/usr/bin/env python3
"""Test multiple subtitle candidates against a video and pick the best sync.

Runs ffsubsync on all candidates in parallel, ranks by absolute offset, and
copies the best match to the output path.

Usage:
    python3 sync_subs.py <video> <candidates_dir> -o <output.srt>
    python3 sync_subs.py <video> <candidates_dir> -o <output.srt> --episode 3
    python3 sync_subs.py <video> <candidates_dir> -o <output.srt> --dry-run

Options:
    --episode N      Filter candidates to those matching episode number N
    --threshold T    Max acceptable offset in seconds (default: 300)
    --workers N      Parallel ffsubsync processes (default: 4)
    --dry-run        Show ranked table without writing output file

Exit codes:
    0  Best match written to --out
    1  No candidate passed the threshold (or --dry-run)

Environment:
    FFSUBSYNC_EXE    Override path to ffsubsync executable
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


def _find_candidates(directory, episode=None):
    candidates = []
    for fname in sorted(os.listdir(directory)):
        if not fname.lower().endswith(SUB_EXTENSIONS):
            continue
        if episode is not None:
            pats = [
                rf"(?<!\d)0*{episode}(?!\d)",
                rf"[Ee]0*{episode}(?!\d)",
                rf"[-_ ]0*{episode}[-_. ]",
            ]
            if not any(re.search(p, fname) for p in pats):
                continue
        candidates.append(os.path.join(directory, fname))
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
    ap.add_argument("candidates_dir", help="Directory of candidate subtitle files")
    ap.add_argument("-o", "--out", required=True, help="Output path for best-synced subtitle (.srt)")
    ap.add_argument("--episode", type=int, default=None,
                    help="Filter candidates to those matching this episode number")
    ap.add_argument("--threshold", type=float, default=300.0,
                    help="Max acceptable absolute offset in seconds (default: 300)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel ffsubsync workers (default: 4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print ranked table without writing output file")
    ap.add_argument("--reference-stream", default=None,
                    help="ffsubsync --reference-stream override (e.g. 'a:1' for second audio track). "
                         "Auto-detected from the video when not set.")
    args = ap.parse_args()

    ffsubsync = os.environ.get("FFSUBSYNC_EXE") or _find_ffsubsync()
    if not ffsubsync:
        sys.exit(
            "ffsubsync not found. Install with: pip install ffsubsync\n"
            "Or set FFSUBSYNC_EXE=/path/to/ffsubsync"
        )

    candidates = _find_candidates(args.candidates_dir, args.episode)
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


if __name__ == "__main__":
    main()
