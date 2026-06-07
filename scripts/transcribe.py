#!/usr/bin/env python3
"""Transcribe a video/audio file to canonical transcript JSON.

Supports two speech-to-text providers, selected with --provider:
  elevenlabs   ElevenLabs Scribe v2 (single synchronous POST)
  soniox       Soniox stt-async-v4 (upload -> create -> poll -> fetch)

Both providers write the SAME canonical JSON shape (the ElevenLabs Scribe
shape), so every downstream tool -- srt_watch.py, srt_translate.py,
srt_summarize.py, and the snoozxy/subs2cia fork -- consumes the output
unchanged regardless of which provider produced it:

    { "language_code": "jpn",
      "text": "<full transcript>",
      "words": [ { "text": "世", "start": 13.72, "end": 13.86,
                   "type": "word", "speaker_id": "speaker_1",
                   "logprob": -0.018 }, ... ] }

`type` is one of "word", "spacing" (whitespace, skipped downstream), or
"audio_event" (music/laughter/etc).

Usage:
    python3 scripts/transcribe.py --provider soniox -o my_video --language ja my_video.mp4

The API key is read from the environment:
    ELEVENLABS_API_KEY   for --provider elevenlabs
    SONIOX_API_KEY       for --provider soniox
"""
import argparse
import json
import math
import os
import sys
import time

import requests


# ── ElevenLabs Scribe ─────────────────────────────────────────────────────────

def transcribe_elevenlabs(audio_path: str, language: str) -> dict:
    """ElevenLabs Scribe v2. Returns the response JSON, which is already in the
    canonical shape, so it is saved verbatim."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is not set.")

    with open(audio_path, "rb") as f:
        resp = requests.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": key},
            data={
                "model_id": "scribe_v2",
                "language_code": language,
                "timestamps_granularity": "word",
                "diarize": "true",
            },
            files={"file": f},
            timeout=3600,
        )
    if not resp.ok:
        sys.exit(f"ElevenLabs error {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ── Soniox ────────────────────────────────────────────────────────────────────

SONIOX_API = "https://api.soniox.com/v1"


def transcribe_soniox(audio_path: str, language: str) -> dict:
    """Soniox stt-async-v4. Runs the async flow and normalizes the token stream
    into the canonical Scribe shape."""
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        sys.exit("SONIOX_API_KEY is not set.")
    auth = {"Authorization": f"Bearer {key}"}

    # 1. upload
    with open(audio_path, "rb") as f:
        r = requests.post(f"{SONIOX_API}/files", headers=auth,
                          files={"file": f}, timeout=3600)
    if not r.ok:
        sys.exit(f"Soniox upload error {r.status_code}: {r.text[:500]}")
    file_id = r.json()["id"]

    # 2. create transcription
    r = requests.post(
        f"{SONIOX_API}/transcriptions",
        headers={**auth, "Content-Type": "application/json"},
        json={
            "model": "stt-async-v4",
            "file_id": file_id,
            "language_hints": [language],
            "enable_speaker_diarization": True,
        },
        timeout=120,
    )
    if not r.ok:
        sys.exit(f"Soniox create error {r.status_code}: {r.text[:500]}")
    tid = r.json()["id"]

    # 3. poll until done
    status = None
    for _ in range(1800):  # up to ~1h at 2s intervals
        r = requests.get(f"{SONIOX_API}/transcriptions/{tid}", headers=auth, timeout=60)
        status = r.json().get("status")
        if status in ("completed", "error"):
            break
        time.sleep(2)
    if status != "completed":
        sys.exit(f"Soniox transcription did not complete: status={status} detail={r.text[:500]}")

    # 4. fetch transcript
    r = requests.get(f"{SONIOX_API}/transcriptions/{tid}/transcript", headers=auth, timeout=120)
    if not r.ok:
        sys.exit(f"Soniox transcript error {r.status_code}: {r.text[:500]}")
    raw = r.json()

    # Best-effort cleanup so we don't leave files/jobs on the account.
    try:
        requests.delete(f"{SONIOX_API}/transcriptions/{tid}", headers=auth, timeout=60)
        requests.delete(f"{SONIOX_API}/files/{file_id}", headers=auth, timeout=60)
    except requests.RequestException:
        pass

    return soniox_to_canonical(raw)


def soniox_to_canonical(raw: dict) -> dict:
    """Convert a Soniox transcript response into the canonical Scribe shape.

    Soniox returns one token per character for Japanese (same granularity as
    Scribe), with ms timestamps and a string speaker id. Mapping:
      start_ms/1000 -> start, end_ms/1000 -> end
      speaker "1"   -> speaker_id "speaker_1"
      is_audio_event-> type "audio_event"; whitespace -> "spacing"; else "word"
      confidence    -> logprob (ln(confidence)), for fidelity; unused downstream
    """
    words = []
    for t in raw.get("tokens", []):
        text = t.get("text", "")
        start = t.get("start_ms", 0) / 1000.0
        end = t.get("end_ms", 0) / 1000.0
        speaker = t.get("speaker")
        speaker_id = f"speaker_{speaker}" if speaker is not None else "speaker_0"
        conf = t.get("confidence")
        logprob = math.log(conf) if conf and conf > 0 else 0.0

        # Soniox glues a leading space onto word tokens in spaced languages
        # (e.g. " area" in English). Japanese never has these, but split them
        # out into a spacing token defensively so concatenation stays clean.
        if t.get("is_audio_event"):
            words.append(_word(text, start, end, "audio_event", speaker_id, logprob))
            continue
        if text.strip() == "":
            words.append(_word(text, start, end, "spacing", speaker_id, logprob))
            continue
        if text != text.lstrip(" "):
            stripped = text.lstrip(" ")
            words.append(_word(" ", start, start, "spacing", speaker_id, 0.0))
            text = stripped
        words.append(_word(text, start, end, "word", speaker_id, logprob))

    full_text = raw.get("text") or "".join(w["text"] for w in words)
    return {"language_code": "jpn", "text": full_text, "words": words}


def _word(text, start, end, typ, speaker_id, logprob):
    return {
        "text": text,
        "start": round(start, 3),
        "end": round(end, 3),
        "type": typ,
        "speaker_id": speaker_id,
        "logprob": round(logprob, 4),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Transcribe to canonical transcript JSON.")
    ap.add_argument("audio", help="Path to the video/audio file.")
    ap.add_argument("--provider", required=True, choices=["elevenlabs", "soniox"])
    ap.add_argument("--language", default="ja", help="Language code (default: ja).")
    ap.add_argument("-o", "--output", help="Output stem (default: input basename).")
    ap.add_argument("--force", action="store_true", help="Overwrite existing output JSON.")
    args = ap.parse_args()

    if not os.path.exists(args.audio):
        sys.exit(f"File not found: {args.audio}")

    stem = args.output or os.path.splitext(os.path.basename(args.audio))[0]
    out_path = f"{stem}.json"

    if os.path.exists(out_path) and not args.force:
        print(f"Skipping transcription — {out_path} already exists. Use --force to overwrite.")
        sys.exit(0)

    if args.provider == "elevenlabs":
        data = transcribe_elevenlabs(args.audio, args.language)
    else:
        data = transcribe_soniox(args.audio, args.language)

    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, out_path)
    print(f"Wrote {out_path} ({len(data.get('words', []))} words, provider={args.provider})")


if __name__ == "__main__":
    main()
