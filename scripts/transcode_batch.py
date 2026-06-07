#!/usr/bin/env python3
"""Batch transcode video files in parallel using ffmpeg.

Transcodes all matching video files in input_dir to output_dir, running
multiple ffmpeg processes concurrently.

Usage:
    python3 transcode_batch.py <input_dir> <output_dir> [options]

Options:
    --height N       Target height in pixels (default: 480). Width is auto (-2).
    --crf N          CRF quality value (default: 23)
    --preset PRESET  x264 encoding preset (default: fast)
    --ext EXT        Output container format (default: mkv)
    --suffix SUFFIX  Suffix appended before extension, e.g. _480p (default: none)
    --glob PATTERN   Filename glob to match inputs (default: *.mkv *.mp4 *.avi)
    --workers N      Parallel ffmpeg processes (default: 2)
    --overwrite      Re-encode files that already exist in output_dir

Examples:
    # Transcode all MKVs to 480p in parallel
    python3 transcode_batch.py "D:/anime/show" "D:/anime/show/480p"

    # 720p with a _720p suffix on output filenames
    python3 transcode_batch.py src/ out/ --height 720 --suffix _720p

Notes:
    - Audio is copied as-is (-c:a copy). Default stream mapping keeps the
      first audio track, which is typically the target language on JP releases.
    - Workers default to 2 because each ffmpeg process is CPU-intensive.
      Set higher only if you have many cores and fast storage.
"""

import argparse
import concurrent.futures
import glob
import os
import subprocess
import sys


def _find_inputs(input_dir, patterns):
    files = []
    for pattern in patterns:
        files.extend(glob.glob(os.path.join(input_dir, pattern)))
    return sorted(set(files))


def _transcode(ffmpeg, src, dst, height, crf, preset, overwrite):
    if os.path.isfile(dst) and not overwrite:
        return (src, dst, "skipped", None)
    cmd = [
        ffmpeg, "-y" if overwrite else "-n",
        "-i", src,
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-c:a", "copy",
        dst,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            return (src, dst, "ok", None)
        return (src, dst, "error", result.stderr[-500:])
    except subprocess.TimeoutExpired:
        return (src, dst, "error", "timeout after 1 hour")
    except Exception as e:
        return (src, dst, "error", str(e))


def main():
    ap = argparse.ArgumentParser(
        description="Batch transcode video files in parallel."
    )
    ap.add_argument("input_dir", help="Directory containing source video files")
    ap.add_argument("output_dir", help="Directory to write transcoded files")
    ap.add_argument("--height", type=int, default=480,
                    help="Target height in pixels (default: 480)")
    ap.add_argument("--crf", type=int, default=23,
                    help="CRF quality (default: 23; lower = higher quality)")
    ap.add_argument("--preset", default="fast",
                    help="x264 preset (default: fast)")
    ap.add_argument("--ext", default="mkv",
                    help="Output container extension (default: mkv)")
    ap.add_argument("--suffix", default="",
                    help="Suffix to add before extension, e.g. _480p")
    ap.add_argument("--glob", dest="globs", action="append",
                    help="Filename glob(s) to match (default: *.mkv *.mp4 *.avi). "
                         "Repeat flag to add multiple patterns.")
    ap.add_argument("--workers", type=int, default=2,
                    help="Parallel ffmpeg processes (default: 2)")
    ap.add_argument("--overwrite", action="store_true",
                    help="Re-encode files that already exist in output_dir")
    args = ap.parse_args()

    import shutil
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg not found. Install from https://ffmpeg.org or via package manager.")

    patterns = args.globs or ["*.mkv", "*.mp4", "*.avi"]
    inputs = _find_inputs(args.input_dir, patterns)
    if not inputs:
        sys.exit(f"No matching files found in {args.input_dir} for patterns: {patterns}")

    os.makedirs(args.output_dir, exist_ok=True)

    jobs = []
    for src in inputs:
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(args.output_dir, f"{stem}{args.suffix}.{args.ext.lstrip('.')}")
        jobs.append((src, dst))

    print(f"Transcoding {len(jobs)} file(s) → {args.output_dir}  "
          f"[{args.height}p, CRF {args.crf}, preset={args.preset}, workers={args.workers}]")

    ok = skipped = errors = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_transcode, ffmpeg, src, dst,
                        args.height, args.crf, args.preset, args.overwrite): (src, dst)
            for src, dst in jobs
        }
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            src, dst, status, err = future.result()
            name = os.path.basename(src)
            if status == "ok":
                ok += 1
                size_mb = os.path.getsize(dst) / 1_048_576
                print(f"  [{done}/{len(jobs)}] {name}  →  {os.path.basename(dst)}  ({size_mb:.0f} MB)")
            elif status == "skipped":
                skipped += 1
                print(f"  [{done}/{len(jobs)}] {name}  skipped (already exists)")
            else:
                errors += 1
                print(f"  [{done}/{len(jobs)}] {name}  ERROR: {err}")

    print(f"\nDone: {ok} transcoded, {skipped} skipped, {errors} errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
