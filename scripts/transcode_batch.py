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
    --profile NAME    Quality/speed preset: default | proxy (480p screenshot proxies)
    --encoder NAME    Video encoder: auto|libx264|h264_nvenc|h264_amf|h264_qsv
    --hw-decode       Enable hardware-accelerated decode (cuda|d3d11va|qsv|auto)
    --copy-if-small   Remux when source height <= target (default: on)
    --no-copy-if-small
    --workers N       Parallel ffmpeg processes (default: from ~/.dojo_hw_cache.json)
    --overwrite       Re-encode files that already exist in output_dir

Examples:
    python3 transcode_batch.py "D:/anime/show" "D:/anime/show/480p" --encoder auto
    python3 transcode_batch.py src/ out/ --profile proxy --encoder auto
    python3 scripts/hw_probe.py   # run once; sets transcode_workers + encoder in cache
"""

import argparse
import concurrent.futures
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HW_CACHE_PATH = Path.home() / ".dojo_hw_cache.json"
_GPU_ENCODERS = ["h264_nvenc", "h264_amf", "h264_qsv"]
_NVENC_MAX_WORKERS = 3  # consumer GPUs typically cap concurrent NVENC sessions


# ── hw cache / probes ───────────────────────────────────────────────────────

def _load_hw_cache():
    """Return recommendations dict from ~/.dojo_hw_cache.json, or {}."""
    try:
        with open(_HW_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f).get("recommendations", {})
    except Exception:
        return {}


def _logical_cores():
    return os.cpu_count() or 4


def _test_encoder(ffmpeg_exe, name):
    """Match hw_probe.py: 256x256 testsrc + nv12 (AMF rejects tiny nullsrc)."""
    r = subprocess.run(
        [
            ffmpeg_exe, "-hide_banner",
            "-f", "lavfi", "-i", "testsrc=size=256x256:duration=0.1:rate=30",
            "-vf", "format=nv12",
            "-c:v", name, "-f", "null", "-",
        ],
        capture_output=True,
        timeout=20,
    )
    return r.returncode == 0


def _test_scale_cuda(ffmpeg_exe):
    r = subprocess.run(
        [
            ffmpeg_exe, "-hide_banner",
            "-f", "lavfi", "-i", "testsrc=size=256x256:duration=0.1:rate=30",
            "-vf", "scale_cuda=128:128,format=nv12",
            "-f", "null", "-",
        ],
        capture_output=True,
        timeout=20,
    )
    return r.returncode == 0


def _resolve_encoder(ffmpeg_exe, requested):
    """Return the encoder name to use. 'auto' checks cache then live-tests."""
    if requested and requested != "auto":
        return requested
    cached = _load_hw_cache().get("FFMPEG_ENCODER")
    if cached and cached != "libx264":
        sys.stderr.write(
            f"Using cached encoder: {cached} (run hw_probe.py --force to refresh)\n"
        )
        return cached
    for enc in _GPU_ENCODERS:
        try:
            if _test_encoder(ffmpeg_exe, enc):
                return enc
        except Exception:
            pass
    return "libx264"


def _is_gpu_encoder(name):
    return name in _GPU_ENCODERS


def _resolve_workers(encoder, explicit):
    """CLI > hw_cache transcode_workers > heuristic. Cap NVENC concurrency."""
    cache = _load_hw_cache()
    if explicit is not None:
        workers = explicit
    else:
        cached = cache.get("transcode_workers")
        try:
            workers = int(cached) if cached is not None else None
        except (TypeError, ValueError):
            workers = None
        if workers is None:
            cores = _logical_cores()
            if _is_gpu_encoder(encoder):
                workers = min(4, cores)
            else:
                workers = max(1, cores // 4)
    if encoder == "h264_nvenc":
        workers = min(workers, _NVENC_MAX_WORKERS)
    return max(1, workers)


def _probe_video_height(ffprobe_exe, path):
    try:
        result = subprocess.run(
            [
                ffprobe_exe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=height",
                "-of", "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass
    return None


# ── encoder-specific ffmpeg args ─────────────────────────────────────────────

def _encode_args(
    encoder,
    height,
    crf,
    preset,
    hwaccel,
    scale_cuda_ok,
    nvenc_preset,
    x264_threads,
):
    """Return ffmpeg args for video encoding."""
    use_cuda_scale = (
        scale_cuda_ok
        and hwaccel == "cuda"
        and encoder in ("h264_nvenc", "hevc_nvenc")
    )
    if use_cuda_scale:
        vf = f"scale_cuda=-2:{height},format=nv12"
    elif encoder == "h264_amf":
        vf = f"scale=-2:{height},format=yuv420p"
    else:
        vf = f"scale=-2:{height}"

    if encoder == "h264_nvenc":
        return [
            "-vf", vf,
            "-c:v", "h264_nvenc", "-preset", nvenc_preset,
            "-rc:v", "vbr", "-cq:v", str(crf), "-b:v", "0",
        ]

    if encoder == "hevc_nvenc":
        return [
            "-vf", vf,
            "-c:v", "hevc_nvenc", "-preset", nvenc_preset,
            "-rc:v", "vbr", "-cq:v", str(crf), "-b:v", "0",
        ]

    if encoder == "h264_amf":
        return [
            "-vf", vf,
            "-c:v", "h264_amf", "-quality", "speed",
            "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf),
        ]

    if encoder == "h264_qsv":
        return [
            "-vf", vf,
            "-c:v", "h264_qsv", "-global_quality", str(crf),
            "-preset", preset,
        ]

    thread_args = ["-threads", str(x264_threads)] if x264_threads > 0 else []
    return [
        "-vf", vf,
        *thread_args,
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
    ]


# ── ffmpeg execution ─────────────────────────────────────────────────────────

def _run_ffmpeg(cmd, timeout=7200):
    """Run ffmpeg without buffering unbounded stderr in memory."""
    stderr_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".ffmpeg.log", delete=False, encoding="utf-8"
        ) as f:
            stderr_path = f.name
        with open(stderr_path, "w", encoding="utf-8", errors="replace") as err_file:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=err_file,
                timeout=timeout,
            )
        if result.returncode == 0:
            return 0, None
        with open(stderr_path, encoding="utf-8", errors="replace") as f:
            return result.returncode, f.read()[-800:]
    except subprocess.TimeoutExpired:
        return -1, "timeout after 2 hours"
    except Exception as e:
        return -1, str(e)
    finally:
        if stderr_path and os.path.isfile(stderr_path):
            try:
                os.unlink(stderr_path)
            except OSError:
                pass


def _remux_copy(ffmpeg_exe, src, dst, overwrite):
    cmd = [
        ffmpeg_exe,
        "-hide_banner", "-loglevel", "error",
        "-y" if overwrite else "-n",
        "-i", src,
        "-map", "0:v:0", "-map", "0:a",
        "-c:v", "copy", "-c:a", "copy",
        dst,
    ]
    return _run_ffmpeg(cmd)


def _transcode(
    ffmpeg_exe,
    ffprobe_exe,
    src,
    dst,
    encoder,
    height,
    crf,
    preset,
    hwaccel,
    scale_cuda_ok,
    nvenc_preset,
    x264_threads,
    copy_if_small,
    overwrite,
):
    if os.path.isfile(dst) and not overwrite:
        return (src, dst, "skipped", None)

    if copy_if_small and ffprobe_exe:
        source_h = _probe_video_height(ffprobe_exe, src)
        if source_h is not None and source_h <= height:
            code, err = _remux_copy(ffmpeg_exe, src, dst, overwrite)
            if code == 0:
                return (src, dst, "copied", None)
            return (src, dst, "error", err)

    hw_args = []
    if hwaccel and hwaccel.lower() not in ("", "none", "off"):
        # AMF + hw decode fails with scale+format filters — software decode is fine.
        if encoder != "h264_amf":
            hw_args = ["-hwaccel", hwaccel]
            if hwaccel == "cuda" and _is_gpu_encoder(encoder):
                hw_args += ["-hwaccel_output_format", "cuda"]

    enc_args = _encode_args(
        encoder, height, crf, preset, hwaccel, scale_cuda_ok,
        nvenc_preset, x264_threads,
    )

    cmd = (
        [ffmpeg_exe, "-hide_banner", "-loglevel", "error"]
        + (["-y"] if overwrite else ["-n"])
        + hw_args
        + ["-i", src]
        + ["-map", "0:v:0", "-map", "0:a"]
        + enc_args
        + ["-c:a", "copy", dst]
    )

    code, err = _run_ffmpeg(cmd)
    if code == 0:
        return (src, dst, "ok", None)
    return (src, dst, "error", err)


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
                    help="Encoder preset for libx264 / QSV (default: fast)")
    ap.add_argument("--profile", choices=["default", "proxy"], default="default",
                    help="proxy = faster 480p proxies (crf 27, veryfast / nvenc p6)")
    ap.add_argument("--encoder", default=None,
                    help="Video encoder: auto|libx264|h264_nvenc|h264_amf|h264_qsv "
                         "(default: FFMPEG_ENCODER env var, or auto)")
    ap.add_argument("--hw-decode", dest="hw_decode", nargs="?", const="auto",
                    default=None,
                    help="Enable hardware decode (cuda|d3d11va|qsv|auto). "
                         "Reads FFMPEG_HWACCEL env var if not specified.")
    ap.add_argument("--ext", default="mkv",
                    help="Output container extension (default: mkv)")
    ap.add_argument("--suffix", default="",
                    help="Suffix to add before extension, e.g. _480p")
    ap.add_argument("--glob", dest="globs", action="append",
                    help="Filename glob(s) to match inputs (default: *.mkv *.mp4 *.avi)")
    ap.add_argument("--workers", type=int, default=None,
                    help="Parallel ffmpeg processes (default: ~/.dojo_hw_cache.json)")
    ap.add_argument("--copy-if-small", action=argparse.BooleanOptionalAction, default=True,
                    help="Remux without re-encoding when source height <= target (default: on)")
    ap.add_argument("--overwrite", action="store_true",
                    help="Re-encode files that already exist in output_dir")
    args = ap.parse_args()

    ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe:
        sys.exit("ffmpeg not found. Install from https://ffmpeg.org")
    ffprobe_exe = shutil.which("ffprobe")

    crf = args.crf
    preset = args.preset
    nvenc_preset = "p4"
    if args.profile == "proxy":
        crf = 27
        preset = "veryfast"
        nvenc_preset = "p6"

    encoder_req = args.encoder or os.environ.get("FFMPEG_ENCODER", "auto")
    if encoder_req == "auto":
        sys.stderr.write("Auto-detecting best encoder...\n")
    encoder = _resolve_encoder(ffmpeg_exe, encoder_req)
    if encoder_req == "auto" and encoder != "libx264":
        sys.stderr.write(f"Using GPU encoder: {encoder}\n")
    elif encoder_req == "auto":
        sys.stderr.write("No GPU encoder found, using libx264\n")

    hwaccel = (
        args.hw_decode
        or os.environ.get("FFMPEG_HWACCEL")
        or _load_hw_cache().get("FFMPEG_HWACCEL", "")
    )
    if hwaccel == "auto":
        hwaccel = (
            "cuda" if encoder == "h264_nvenc"
            else "d3d11va" if encoder == "h264_amf"
            else "qsv" if encoder == "h264_qsv"
            else ""
        )

    workers = _resolve_workers(encoder, args.workers)
    x264_threads = max(1, _logical_cores() // workers) if encoder == "libx264" else 0

    scale_cuda_ok = False
    if hwaccel == "cuda" and encoder in ("h264_nvenc", "hevc_nvenc"):
        scale_cuda_ok = _test_scale_cuda(ffmpeg_exe)
        if scale_cuda_ok:
            sys.stderr.write("Using scale_cuda for GPU decode/scale/encode pipeline\n")
        else:
            sys.stderr.write(
                "scale_cuda unavailable; falling back to CPU scale with CUDA decode\n"
            )

    patterns = args.globs or ["*.mkv", "*.mp4", "*.avi"]
    inputs = []
    for pattern in patterns:
        inputs.extend(glob.glob(os.path.join(glob.escape(args.input_dir), pattern)))
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
    profile_label = f", profile={args.profile}" if args.profile != "default" else ""
    print(f"Transcoding {len(jobs)} file(s) → {args.output_dir}")
    print(
        f"  encoder={encoder}, {args.height}p, quality={crf}, preset={preset}, "
        f"nvenc_preset={nvenc_preset}{hw_label}{profile_label}, workers={workers}"
    )
    if encoder == "libx264":
        print(f"  libx264 threads per job: {x264_threads}")
    if args.copy_if_small:
        print("  copy-if-small: on (remux when source height <= target)")

    ok = copied = skipped = errors = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _transcode,
                ffmpeg_exe,
                ffprobe_exe,
                src,
                dst,
                encoder,
                args.height,
                crf,
                preset,
                hwaccel,
                scale_cuda_ok,
                nvenc_preset,
                x264_threads,
                args.copy_if_small,
                args.overwrite,
            ): (src, dst)
            for src, dst in jobs
        }
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            src, dst, status, err = future.result()
            name = os.path.basename(src)
            if status in ("ok", "copied"):
                if status == "ok":
                    ok += 1
                else:
                    copied += 1
                size_mb = os.path.getsize(dst) / 1_048_576
                label = "remuxed" if status == "copied" else "encoded"
                print(
                    f"  [{done}/{len(jobs)}] {name}  →  {os.path.basename(dst)}  "
                    f"({size_mb:.0f} MB, {label})"
                )
            elif status == "skipped":
                skipped += 1
                print(f"  [{done}/{len(jobs)}] {name}  skipped (already exists)")
            else:
                errors += 1
                print(f"  [{done}/{len(jobs)}] {name}  ERROR")
                if err:
                    for line in err.splitlines()[-5:]:
                        print(f"    {line}")

    print(f"\nDone: {ok} encoded, {copied} remuxed, {skipped} skipped, {errors} errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
