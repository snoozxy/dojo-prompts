#!/usr/bin/env python3
"""Batch transcode video files in parallel using ffmpeg.

Transcodes all matching video files in input_dir to output_dir, running
multiple ffmpeg processes concurrently. Supports GPU-accelerated encoding via
NVENC (NVIDIA), AMF (AMD), or QuickSync (Intel) when available.

Usage:
    python3 transcode_batch.py <input_dir> <output_dir> [options]

Options:
    --height N        Target height in pixels (default: 480). Width is auto (-2).
    --crf N           CRF/quality value (default: 23)
    --preset PRESET   Encoder preset (default: fast). Meaning varies by encoder.
    --encoder NAME    Video encoder: auto|libx264|h264_nvenc|h264_amf|h264_qsv
                      auto detects the best available GPU encoder, falls back to
                      libx264. Reads FFMPEG_ENCODER env var as default.
    --hw-decode       Enable hardware-accelerated decode (faster seeking in large
                      files). Reads FFMPEG_HWACCEL env var; set to cuda, d3d11va,
                      or qsv. Omit or set to 'none' for software decode.
    --ext EXT         Output container format (default: mkv)
    --suffix SUFFIX   Suffix appended before extension, e.g. _480p (default: none)
    --glob PATTERN    Filename glob to match inputs (default: *.mkv *.mp4 *.avi).
                      Repeat to add multiple patterns.
    --workers N       Parallel ffmpeg processes. Default: 2 for CPU encoders,
                      4 for GPU encoders (GPU offloads the encode bottleneck).
    --overwrite       Re-encode files that already exist in output_dir

Examples:
    # Auto-detect best encoder (GPU if available)
    python3 transcode_batch.py "D:/anime/show" "D:/anime/show/480p" --encoder auto

    # Force NVENC with hardware decode
    python3 transcode_batch.py src/ out/ --encoder h264_nvenc --hw-decode

    # 720p software transcode
    python3 transcode_batch.py src/ out/ --height 720 --suffix _720p

    # Run hw_probe.py first to find your optimal flags:
    python3 scripts/hw_probe.py

Notes:
    - Audio is copied as-is (-c:a copy). Default stream mapping keeps the first
      audio track, which is typically the target language on JP releases.
    - GPU encoders (nvenc/amf/qsv) are significantly faster than libx264 for
      large batches or 10hr+ content, with minimal quality difference at CRF 23.
    - Set FFMPEG_ENCODER and FFMPEG_HWACCEL in your shell profile to avoid
      passing flags every time. hw_probe.py tells you the right values.
"""

import argparse
import concurrent.futures
import glob
import os
import shutil
import subprocess
import sys


# ── encoder detection ────────────────────────────────────────────────────────

_GPU_ENCODERS = ["h264_nvenc", "h264_amf", "h264_qsv"]


def _test_encoder(name):
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "nullsrc=s=64x64",
         "-t", "0.1", "-c:v", name, "-f", "null", "-"],
        capture_output=True, timeout=15,
    )
    return r.returncode == 0


def _resolve_encoder(requested):
    """Return the encoder name to use. 'auto' picks best available GPU encoder."""
    if requested and requested != "auto":
        return requested
    for enc in _GPU_ENCODERS:
        try:
            if _test_encoder(enc):
                return enc
        except Exception:
            pass
    return "libx264"


def _is_gpu_encoder(name):
    return name in _GPU_ENCODERS


# ── encoder-specific ffmpeg args ─────────────────────────────────────────────

def _encode_args(encoder, height, crf, preset):
    """Return the ffmpeg args for video encoding with the given encoder."""
    vf = f"scale=-2:{height}"

    if encoder == "h264_nvenc":
        # VBR constant-quality mode — closest equivalent to CRF in nvenc
        return ["-vf", vf, "-c:v", "h264_nvenc", "-preset", "p4",
                "-rc:v", "vbr", "-cq:v", str(crf), "-b:v", "0"]

    if encoder == "hevc_nvenc":
        return ["-vf", vf, "-c:v", "hevc_nvenc", "-preset", "p4",
                "-rc:v", "vbr", "-cq:v", str(crf), "-b:v", "0"]

    if encoder == "h264_amf":
        # AMD VBR quality mode
        return ["-vf", vf, "-c:v", "h264_amf", "-quality", "speed",
                "-rc", "vbr_latency", "-qp_i", str(crf), "-qp_p", str(crf)]

    if encoder == "h264_qsv":
        return ["-vf", vf, "-c:v", "h264_qsv", "-global_quality", str(crf),
                "-preset", preset]

    # libx264 (default)
    return ["-vf", vf, "-c:v", "libx264", "-crf", str(crf), "-preset", preset]


# ── transcode ────────────────────────────────────────────────────────────────

def _transcode(ffmpeg_exe, src, dst, encoder, height, crf, preset, hwaccel, overwrite):
    if os.path.isfile(dst) and not overwrite:
        return (src, dst, "skipped", None)

    hw_args = []
    if hwaccel and hwaccel.lower() not in ("", "none", "off"):
        hw_args = ["-hwaccel", hwaccel]
        # For CUDA, keep decoded frames in GPU memory for the scale filter
        if hwaccel == "cuda" and _is_gpu_encoder(encoder):
            hw_args += ["-hwaccel_output_format", "cuda"]

    enc_args = _encode_args(encoder, height, crf, preset)

    cmd = (
        [ffmpeg_exe, "-y" if overwrite else "-n"]
        + hw_args
        + ["-i", src]
        + enc_args
        + ["-c:a", "copy", dst]
    )

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode == 0:
            return (src, dst, "ok", None)
        return (src, dst, "error", result.stderr[-800:])
    except subprocess.TimeoutExpired:
        return (src, dst, "error", "timeout after 2 hours")
    except Exception as e:
        return (src, dst, "error", str(e))


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Batch transcode video files in parallel with optional GPU acceleration."
    )
    ap.add_argument("input_dir", help="Directory containing source video files")
    ap.add_argument("output_dir", help="Directory to write transcoded files")
    ap.add_argument("--height", type=int, default=480,
                    help="Target height in pixels (default: 480)")
    ap.add_argument("--crf", type=int, default=23,
                    help="Quality value (default: 23; lower = better quality)")
    ap.add_argument("--preset", default="fast",
                    help="Encoder preset (default: fast)")
    ap.add_argument("--encoder", default=None,
                    help="Video encoder: auto|libx264|h264_nvenc|h264_amf|h264_qsv "
                         "(default: FFMPEG_ENCODER env var, or libx264)")
    ap.add_argument("--hw-decode", dest="hw_decode", nargs="?", const="auto",
                    default=None,
                    help="Enable hardware decode (cuda|d3d11va|qsv|auto). "
                         "Reads FFMPEG_HWACCEL env var if not specified.")
    ap.add_argument("--ext", default="mkv",
                    help="Output container extension (default: mkv)")
    ap.add_argument("--suffix", default="",
                    help="Suffix to add before extension, e.g. _480p")
    ap.add_argument("--glob", dest="globs", action="append",
                    help="Filename glob(s) to match inputs (default: *.mkv *.mp4 *.avi). "
                         "Repeat to add more patterns.")
    ap.add_argument("--workers", type=int, default=None,
                    help="Parallel ffmpeg processes (default: 2 for CPU, 4 for GPU)")
    ap.add_argument("--overwrite", action="store_true",
                    help="Re-encode files that already exist in output_dir")
    args = ap.parse_args()

    ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe:
        sys.exit("ffmpeg not found. Install from https://ffmpeg.org")

    # Resolve encoder: CLI > env var > auto-detect
    encoder_req = args.encoder or os.environ.get("FFMPEG_ENCODER", "auto")
    if encoder_req == "auto":
        sys.stderr.write("Auto-detecting best encoder...\n")
    encoder = _resolve_encoder(encoder_req)
    if encoder_req == "auto" and encoder != "libx264":
        sys.stderr.write(f"Using GPU encoder: {encoder}\n")
    elif encoder_req == "auto":
        sys.stderr.write("No GPU encoder found, using libx264\n")

    # Resolve hwaccel: CLI > env var
    hwaccel = args.hw_decode or os.environ.get("FFMPEG_HWACCEL", "")
    if hwaccel == "auto":
        hwaccel = "cuda" if encoder == "h264_nvenc" else \
                  "d3d11va" if encoder in ("h264_amf",) else \
                  "qsv" if encoder == "h264_qsv" else ""

    # Default workers based on encoder type
    if args.workers is not None:
        workers = args.workers
    elif _is_gpu_encoder(encoder):
        workers = 4   # GPU handles encoding; CPU isn't the bottleneck
    else:
        workers = 2   # CPU encoding is expensive; don't oversaturate

    patterns = args.globs or ["*.mkv", "*.mp4", "*.avi"]
    inputs = []
    for pattern in patterns:
        inputs.extend(glob.glob(os.path.join(args.input_dir, pattern)))
    inputs = sorted(set(inputs))

    if not inputs:
        sys.exit(f"No matching files found in {args.input_dir} for patterns: {patterns}")

    os.makedirs(args.output_dir, exist_ok=True)

    jobs = []
    for src in inputs:
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(args.output_dir, f"{stem}{args.suffix}.{args.ext.lstrip('.')}")
        jobs.append((src, dst))

    hw_label = f", hwaccel={hwaccel}" if hwaccel and hwaccel not in ("", "none") else ""
    print(f"Transcoding {len(jobs)} file(s) → {args.output_dir}")
    print(f"  encoder={encoder}, {args.height}p, quality={args.crf}, "
          f"preset={args.preset}{hw_label}, workers={workers}")

    ok = skipped = errors = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _transcode, ffmpeg_exe, src, dst,
                encoder, args.height, args.crf, args.preset, hwaccel, args.overwrite
            ): (src, dst)
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
                print(f"  [{done}/{len(jobs)}] {name}  ERROR")
                if err:
                    # Print last few lines of ffmpeg stderr for diagnosis
                    for line in err.splitlines()[-5:]:
                        print(f"    {line}")

    print(f"\nDone: {ok} transcoded, {skipped} skipped, {errors} errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
