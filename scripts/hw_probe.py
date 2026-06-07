#!/usr/bin/env python3
"""Probe system hardware and print recommended settings for subs2cia/ffmpeg.

Usage:
    python3 hw_probe.py           # print human-readable report
    python3 hw_probe.py --json    # print machine-readable JSON

Output includes CPU, RAM, GPU, available ffmpeg encoders, and the exact env
vars / flags to set for maximum performance.

Environment vars the tools will read (set these once in your shell profile):
    SUBS2CIA_WORKERS=N       CardExport parallel workers (default: cpu_count)
    SUBS2CIA_HWACCEL=cuda    Hardware accelerator for ffmpeg decode (cuda | d3d11va | none)
    FFMPEG_ENCODER=h264_nvenc  Hardware encoder for transcode_batch.py
    FFMPEG_HWACCEL=cuda      Hardware accelerator for transcode_batch.py decode
"""

import json
import os
import platform
import subprocess
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(*args, timeout=10):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


def _powershell(script, timeout=10):
    out, rc = _run("powershell", "-NoProfile", "-Command", script, timeout=timeout)
    return out if rc == 0 else ""


# ── CPU ───────────────────────────────────────────────────────────────────────

def probe_cpu():
    cores = os.cpu_count() or 1
    name = platform.processor() or "unknown"
    if platform.system() == "Windows":
        ps = _powershell(
            "Get-CimInstance Win32_Processor | "
            "Select-Object -ExpandProperty Name | "
            "Select-Object -First 1"
        )
        if ps:
            name = ps.strip()
        cores_ps = _powershell(
            "(Get-CimInstance Win32_Processor | "
            "Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum"
        )
        if cores_ps.strip().isdigit():
            cores = int(cores_ps.strip())
    return {"name": name, "logical_cores": cores}


# ── RAM ───────────────────────────────────────────────────────────────────────

def probe_ram():
    total_mb = None
    if platform.system() == "Windows":
        out = _powershell(
            "(Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize"
        )
        if out.strip().isdigit():
            total_mb = int(out.strip()) // 1024
    elif platform.system() in ("Linux", "Darwin"):
        out, _ = _run("grep", "MemTotal", "/proc/meminfo")
        if out:
            import re
            m = re.search(r"(\d+)", out)
            if m:
                total_mb = int(m.group(1)) // 1024
    return {"total_mb": total_mb}


# ── GPU ───────────────────────────────────────────────────────────────────────

def _probe_nvidia():
    out, rc = _run(
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader",
        timeout=8,
    )
    if rc != 0 or not out:
        return None
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            vram_str = parts[1].replace(" MiB", "").replace(" MB", "").strip()
            gpus.append({
                "vendor": "NVIDIA",
                "name": parts[0],
                "vram_mb": int(vram_str) if vram_str.isdigit() else None,
                "driver": parts[2] if len(parts) > 2 else None,
            })
    return gpus or None


def _probe_gpu_wmi():
    if platform.system() != "Windows":
        return None
    out = _powershell(
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name, AdapterRAM, DriverVersion | "
        "ConvertTo-Json"
    )
    if not out:
        return None
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        gpus = []
        for g in data:
            name = g.get("Name", "")
            vram = g.get("AdapterRAM")
            vram_mb = int(vram) // 1048576 if vram else None
            vendor = "NVIDIA" if "NVIDIA" in name or "GeForce" in name or "RTX" in name or "GTX" in name \
                else "AMD" if "AMD" in name or "Radeon" in name \
                else "Intel" if "Intel" in name \
                else "unknown"
            gpus.append({
                "vendor": vendor,
                "name": name,
                "vram_mb": vram_mb,
                "driver": g.get("DriverVersion"),
            })
        return gpus or None
    except (json.JSONDecodeError, TypeError):
        return None


def probe_gpus():
    gpus = _probe_nvidia() or _probe_gpu_wmi() or []
    return gpus


# ── ffmpeg encoders / hwaccels ────────────────────────────────────────────────

_ENCODERS = [
    ("h264_nvenc",  "NVIDIA NVENC (H.264)"),
    ("hevc_nvenc",  "NVIDIA NVENC (H.265/HEVC)"),
    ("h264_amf",    "AMD AMF (H.264)"),
    ("hevc_amf",    "AMD AMF (H.265/HEVC)"),
    ("h264_qsv",    "Intel QuickSync (H.264)"),
    ("hevc_qsv",    "Intel QuickSync (H.265/HEVC)"),
]

_HWACCELS = [
    ("cuda",    "NVIDIA CUDA / NVDEC"),
    ("d3d11va", "Windows D3D11VA (any GPU)"),
    ("dxva2",   "Windows DXVA2 (any GPU, older API)"),
    ("qsv",     "Intel QuickSync decode"),
]


def _ffmpeg_available():
    _, rc = _run("ffmpeg", "-version", timeout=5)
    return rc == 0


def test_encoder(name):
    """Return True if ffmpeg can use this encoder."""
    _, rc = _run(
        "ffmpeg", "-hide_banner",
        "-f", "lavfi", "-i", "nullsrc=s=64x64",
        "-t", "0.1", "-c:v", name, "-f", "null", "-",
        timeout=15,
    )
    return rc == 0


def test_hwaccel(name):
    """Return True if ffmpeg accepts this hwaccel (best-effort — not all accels
    fail at init without a real encoded stream)."""
    _, rc = _run(
        "ffmpeg", "-hide_banner",
        "-hwaccel", name,
        "-f", "lavfi", "-i", "nullsrc=s=64x64",
        "-t", "0.1", "-f", "null", "-",
        timeout=10,
    )
    return rc == 0


def probe_ffmpeg():
    if not _ffmpeg_available():
        return {"available": False, "encoders": {}, "hwaccels": {}}
    encoders = {name: test_encoder(name) for name, _ in _ENCODERS}
    hwaccels = {name: test_hwaccel(name) for name, _ in _HWACCELS}
    return {"available": True, "encoders": encoders, "hwaccels": hwaccels}


# ── recommendations ──────────────────────────────────────────────────────────

def recommend(cpu, ram, gpus, ffmpeg_info):
    recs = {}

    # Workers for CardExport (subs2cia)
    cores = cpu["logical_cores"]
    recs["SUBS2CIA_WORKERS"] = str(cores)

    # ffmpeg encoder
    enc = ffmpeg_info.get("encoders", {})
    if enc.get("h264_nvenc"):
        recs["FFMPEG_ENCODER"] = "h264_nvenc"
    elif enc.get("h264_amf"):
        recs["FFMPEG_ENCODER"] = "h264_amf"
    elif enc.get("h264_qsv"):
        recs["FFMPEG_ENCODER"] = "h264_qsv"
    else:
        recs["FFMPEG_ENCODER"] = "libx264"

    # hw decode accel
    hw = ffmpeg_info.get("hwaccels", {})
    if enc.get("h264_nvenc") and hw.get("cuda"):
        accel = "cuda"
    elif hw.get("d3d11va"):
        accel = "d3d11va"
    elif hw.get("qsv") and enc.get("h264_qsv"):
        accel = "qsv"
    else:
        accel = "none"
    recs["SUBS2CIA_HWACCEL"] = accel
    recs["FFMPEG_HWACCEL"] = accel

    # transcode_batch workers: GPU can handle more parallel encodes
    if recs["FFMPEG_ENCODER"] != "libx264":
        recs["transcode_workers"] = min(4, cores)
    else:
        recs["transcode_workers"] = max(1, cores // 4)

    return recs


# ── report ────────────────────────────────────────────────────────────────────

def full_probe():
    sys.stderr.write("Probing hardware...\n")
    cpu = probe_cpu()
    ram = probe_ram()
    gpus = probe_gpus()
    sys.stderr.write("Testing ffmpeg encoders and hwaccels...\n")
    ffmpeg_info = probe_ffmpeg()
    recs = recommend(cpu, ram, gpus, ffmpeg_info)
    return {
        "cpu": cpu,
        "ram": ram,
        "gpus": gpus,
        "ffmpeg": ffmpeg_info,
        "recommendations": recs,
    }


def print_report(data):
    cpu = data["cpu"]
    ram = data["ram"]
    gpus = data["gpus"]
    ff = data["ffmpeg"]
    recs = data["recommendations"]

    print("=" * 60)
    print("Hardware Report")
    print("=" * 60)

    print(f"\nCPU:  {cpu['name']}")
    print(f"      {cpu['logical_cores']} logical cores")

    if ram["total_mb"]:
        print(f"RAM:  {ram['total_mb'] / 1024:.1f} GB")

    if gpus:
        print("\nGPU(s):")
        for g in gpus:
            vram = f"  {g['vram_mb'] / 1024:.1f} GB VRAM" if g["vram_mb"] else ""
            print(f"  {g['vendor']:8s}  {g['name']}{vram}")
    else:
        print("\nGPU:  not detected")

    if not ff["available"]:
        print("\nffmpeg: NOT FOUND")
    else:
        print("\nffmpeg encoders:")
        for name, label in _ENCODERS:
            tick = "✓" if ff["encoders"].get(name) else "✗"
            print(f"  {tick}  {name:<16}  {label}")
        print("\nffmpeg hwaccels:")
        for name, label in _HWACCELS:
            tick = "✓" if ff["hwaccels"].get(name) else "✗"
            print(f"  {tick}  {name:<12}  {label}")

    print("\n" + "=" * 60)
    print("Recommended settings")
    print("=" * 60)
    print("\nAdd these to your shell profile (PowerShell $PROFILE or .bashrc):\n")
    for k, v in recs.items():
        if not k.startswith("transcode_"):
            print(f'  $env:{k} = "{v}"')

    enc = recs["FFMPEG_ENCODER"]
    workers = recs["transcode_workers"]
    hw = recs["FFMPEG_HWACCEL"]
    hw_flag = f" --hw-decode" if hw != "none" else ""
    print(f"\ntranscode_batch.py flags:")
    print(f"  python3 scripts/transcode_batch.py <in> <out> --encoder {enc} --workers {workers}{hw_flag}")
    print()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Probe system hardware for ffmpeg/subs2cia settings.")
    ap.add_argument("--json", action="store_true", help="Output raw JSON instead of report")
    args = ap.parse_args()

    data = full_probe()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_report(data)


if __name__ == "__main__":
    main()
