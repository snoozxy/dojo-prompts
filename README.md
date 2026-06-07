# dojo-prompts (snoozxy fork)

Fork of [mattvsjapan/dojo-prompts](https://github.com/mattvsjapan/dojo-prompts). See upstream for full documentation and the original skills.

## Changes vs upstream

### New skill: process-local

Workflow for locally stored video (downloaded anime, movies, dramas). Trigger with `/process-local` or say "process local files".

1. Searches [jimaku.cc](https://jimaku.cc) for Japanese subtitles
2. Tests all subtitle candidates in parallel with ffsubsync, picks best match
3. Falls back to AI transcription (ElevenLabs / Soniox) if no good subtitle found
4. Generates a subs2cia Anki deck with optional 480p transcode for screenshots

Requires `$JIMAKU_API_KEY` (generate at jimaku.cc/account).

### New scripts

| Script | Purpose |
|---|---|
| `scripts/jimaku_dl.py` | Search, list, and download subtitles from jimaku.cc |
| `scripts/sync_subs.py` | Test multiple subtitle candidates against a video in parallel; copy best match |
| `scripts/transcode_batch.py` | Parallel ffmpeg transcoding with GPU encoder auto-detection (NVENC / AMF / QSV) |
| `scripts/hw_probe.py` | Detect CPU, RAM, GPU, and available ffmpeg encoders; caches results to `~/.dojo_hw_cache.json` |
| `scripts/combine_tsv.py` | Combine multiple subs2cia TSV files (cross-platform; replaces broken `head -q` one-liner) |

### yt-dlp optimizations

`process-content.md` and CLAUDE.md now use `--concurrent-fragments 4 --retries 10 --fragment-retries 10` for faster, more resilient downloads. Batch downloads use `--download-archive` to skip already-downloaded files.

### Resilient transcription (`scripts/transcribe.py`)

- **Skip if done**: exits early if output JSON already exists (re-run safe). Use `--force` to overwrite.
- **Atomic writes**: writes to `.tmp` then renames, so a crash never leaves a corrupt JSON.
- **Soniox job resumption**: saves `file_id` and `transcription_id` to `<stem>.transcription_state.json` immediately after each API step. A crash mid-transcription resumes from where it left off rather than re-uploading.
- **Poll backoff**: 2s intervals for the first 60s, then 10s — reduces API calls on long files.

### Hardware-aware performance (via `hw_probe.py`)

Run once per machine to detect your GPU and optimal settings:
```bash
python3 dojo-prompts/scripts/hw_probe.py
```

After this, `transcode_batch.py` and [snoozxy/subs2cia](https://github.com/snoozxy/subs2cia) automatically use GPU encoding and hardware-accelerated decode from the cache — no env vars needed.

### subs2cia fork

Requires [snoozxy/subs2cia](https://github.com/snoozxy/subs2cia) — not the upstream fork. Install with:
```bash
pip install git+https://github.com/snoozxy/subs2cia.git
```
