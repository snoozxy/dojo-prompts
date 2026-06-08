# Dojo Prompts

This project contains AI skills for immersion-based Japanese learning. The skill files are located in the `dojo-prompts/` folder (the cloned repo).

## Skills

When the user asks about any of the following, read the corresponding skill file and follow its instructions:

| User says something like... | Skill file |
|---|---|
| "discover content", "find something to watch", "help me find content" | `dojo-prompts/content-discovery.md` |
| "process this video", "download and transcribe", "process content", "YouTube video" | `dojo-prompts/process-content.md` |
| "process local", "local video", "local file", "downloaded anime", "downloaded movie", "local content", "make an Anki deck from this anime", "study this anime", "create a deck for [show]", "make flashcards from [show]" | `dojo-prompts/process-local.md` |
| "create subtitles", "transcribe this", "make an SRT", "generate subs" | `dojo-prompts/create-srt.md` |
| "translate subtitles", "translate this SRT", "make English subs" | `dojo-prompts/translate-srt.md` |
| "make an Anki deck" (with existing SRT/TSV already prepared), "subs2srs", "package the deck", "export apkg" | `dojo-prompts/anki.md` |
| "style guide", "language parent", "analyze their speech" | `dojo-prompts/style-guide.md` |
| "find my mistakes", "analyze my output", "what am I doing wrong" | `dojo-prompts/find-mistakes.md` |
| "condensed audio", "condense this", "passive listening" | `dojo-prompts/condensed-audio.md` |
| "primed summaries", "summarize subs", "english previews", "primed listening summaries" | `dojo-prompts/primed-summaries.md` |
| "download a video", "download this" | Use yt-dlp (see below) |

When a skill is triggered, read the full skill file first, then follow its workflow step by step.

**Routing tip — "Anki deck from anime":** This almost always means `process-local.md` (the full pipeline). `anki.md` is only for the final packaging step when the user already has processed SRT or TSV files. If unsure, ask: *"Do you have the video downloaded locally, or do you have a URL?"*

## Anki deck from downloaded anime — full pipeline

This is the most common multi-step request. The pipeline is:

```
local video files
  → [hw_probe.py]        check GPU / worker settings (once per machine)
  → [transcode_batch.py] optional 480p transcode for smaller screenshots
  → [jimaku_dl.py]       download JP subtitle candidates from jimaku.cc
  → [sync_subs.py]       test all candidates in parallel, pick best sync
  → [subs2cia srs]       export audio clips + screenshots → TSV per episode
  → [prepend_summary.py] prepend episode summary to context column of each TSV
  → [combine_tsv.py]     merge all episode TSVs into combined.tsv
  → [apkg_export.py]     package into final .apkg for Anki import
```

**Don't skip the episode summary step.** It prepends a translation briefing to every card's context column, which makes LLM-assisted study much more useful. Read `anki.md` for the summary format.

Key commands (fill in actual paths):
```bash
# 1. Hardware check (once — skip if ~/.dojo_hw_cache.json exists and is recent)
python3 dojo-prompts/scripts/hw_probe.py --check || python3 dojo-prompts/scripts/hw_probe.py

# 2. Optional 480p transcode (recommended for Anki — much smaller files)
python3 dojo-prompts/scripts/transcode_batch.py "$VIDEO_DIR" "$VIDEO_DIR/480p" --encoder auto

# 3. Search jimaku.cc for subtitles (requires $JIMAKU_API_KEY)
python3 dojo-prompts/scripts/jimaku_dl.py search "Show Name"
python3 dojo-prompts/scripts/jimaku_dl.py download <entry_id> --out subs_download/

# 4. Sync subtitles (test all candidates, pick best)
python3 dojo-prompts/scripts/sync_subs.py video_01.mkv subs_download/ \
  -o synced_subs/show_01.srt --episode 1

# 5. Identify Japanese audio (and subtitle) stream indices — ALWAYS do this first.
# Many encodes put English audio at stream 0. Never assume 0 is Japanese.
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "$VIDEO_DIR/480p/show_01.mkv"
# If using embedded subtitle tracks, also run:
# ffprobe -v error -select_streams s \
#   -show_entries stream=index:stream_tags=language,title \
#   -of csv=p=0 "$VIDEO_DIR/480p/show_01.mkv"
#
# Then run subs2cia with the Japanese stream index (-ai is always required).
# srs reads audio directly from the container (no FLAC demux). Use -b -j for multi-episode batches.
# hw_probe.py caches SUBS2CIA_WORKERS, SUBS2CIA_JOBS, and SUBS2CIA_HWACCEL — no env vars needed.
PYTHONUTF8=1 subs2cia srs -b -j 4 \
  -i "$VIDEO_DIR/480p/show_01.mkv" synced_subs/show_01.srt \
  -ai <jp_audio_index> -p 500 -N -d out_srs --export-header-row

# 6. Generate episode summary and prepend to context column
python3 dojo-prompts/scripts/prepend_summary.py out_srs/show_01.tsv "EPISODE_SUMMARY"

# 7. Combine all TSVs
python3 dojo-prompts/scripts/combine_tsv.py out_srs/ out_srs/combined.tsv

# 8. Export .apkg
python3 dojo-prompts/scripts/apkg_export.py \
  out_srs/combined.tsv out_srs/ "show_name" "$VIDEO_DIR"
```

After the .apkg is created, delete the `out_srs/` directory — everything is embedded in the package.

## Scripts reference

All helper scripts live in `dojo-prompts/scripts/`:

| Script | What it does |
|---|---|
| `hw_probe.py` | Detect CPU/RAM/GPU, test ffmpeg encoders, cache to `~/.dojo_hw_cache.json`. Run once per machine. |
| `transcode_batch.py` | Parallel ffmpeg transcode with GPU auto-detection (`--encoder auto`). |
| `jimaku_dl.py` | Search, list files, and download subtitles from jimaku.cc. |
| `sync_subs.py` | Test subtitle candidates against a video in parallel, copy best match. |
| `combine_tsv.py` | Merge multiple subs2cia TSVs into one (cross-platform). |
| `prepend_summary.py` | Prepend an episode summary string to every row's context column. |
| `apkg_export.py` | Package combined TSV + media into an Anki `.apkg` file. |
| `transcribe.py` | Transcribe audio/video via ElevenLabs Scribe or Soniox. Resumes after crashes. |

## Windows notes

On Windows, `subs2cia` and `ffsubsync` are installed in the Python user Scripts directory and are **not on the Git Bash PATH**. Use the full executable path:

```bash
SUBS2CIA="C:/Users/snoozy/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/Scripts/subs2cia.exe"
FFSUBSYNC="C:/Users/snoozy/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/Scripts/ffsubsync.exe"
```

`sync_subs.py` finds `ffsubsync.exe` automatically using this path — no manual path needed when using the script. Always prefix `subs2cia` commands with `PYTHONUTF8=1` to handle Japanese filenames and subtitles on Windows.

## Hardware profile (one-time setup)

Before running `process-local` or `anki` skills (any workflow that transcodes video or runs subs2cia on a large batch), check whether the hardware cache exists:

```bash
python3 dojo-prompts/scripts/hw_probe.py --check
```

- If it exits **0** (cache is fresh): nothing to do — `transcode_batch.py` and subs2cia read from `~/.dojo_hw_cache.json` automatically.
- If it exits **1** (missing or stale): run the full probe once. It takes ~30 seconds and caches results for 30 days:
  ```bash
  python3 dojo-prompts/scripts/hw_probe.py
  ```

After the cache exists, all tools auto-use the detected GPU encoder, hardware decode accelerator, and subs2cia parallelism settings — no env vars needed. The cache stores:

| Key | Purpose |
|---|---|
| `SUBS2CIA_HWACCEL` | GPU decode for screenshot seeks |
| `SUBS2CIA_WORKERS` | Per-episode card export threads |
| `SUBS2CIA_JOBS` | Parallel episodes in batch mode (`-b -j`) |
| `FFMPEG_ENCODER` / `FFMPEG_HWACCEL` | Used by `transcode_batch.py` |

When `SUBS2CIA_JOBS > 1`, per-episode card workers scale down automatically (`WORKERS // JOBS`).

## Dependency checks

Before running any skill, check that required programs are installed. **Only install something if it's missing.** Do not reinstall programs that are already present.

Check with `which` or `command -v` for CLI tools, and `pip show` for Python packages:

```bash
# CLI tools
command -v yt-dlp >/dev/null 2>&1 || echo "MISSING: yt-dlp"
command -v ffprobe >/dev/null 2>&1 || echo "MISSING: ffprobe"

# Python packages
pip show fugashi >/dev/null 2>&1 || echo "MISSING: fugashi"
pip show genanki >/dev/null 2>&1 || echo "MISSING: genanki"
pip show requests >/dev/null 2>&1 || echo "MISSING: requests"

# process-local specific
pip show ffsubsync >/dev/null 2>&1 || echo "MISSING: ffsubsync (needed for process-local)"
```

**Special case — subs2cia:** Even if subs2cia is installed, you must verify it's the correct fork **and that it's up to date**. Check with:
```bash
pip show subs2cia 2>/dev/null | grep -i "home-page\|location"
```
If the installed version is NOT from `github.com/snoozxy/subs2cia`, uninstall it and install the correct fork:
```bash
pip uninstall -y subs2cia
pip install git+https://github.com/snoozxy/subs2cia.git
```
If it IS the correct fork, upgrade it to ensure you have the latest features:
```bash
pip install --upgrade git+https://github.com/snoozxy/subs2cia.git
```

If a required tool is missing, just install it and move on. No need to ask — but don't reinstall things that are already there.

## Important

- **Downloading videos**: Always use yt-dlp and always download as MP4. After downloading, rename files with a romanized version of the full title (see `process-content.md` for detailed naming rules):
  ```bash
  # Single video
  yt-dlp -f "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]" --merge-output-format mp4 \
    --concurrent-fragments 4 --retries 10 --fragment-retries 10 --no-playlist \
    -o "%(title)s.%(ext)s" "URL"
  # Playlist or channel
  yt-dlp -f "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]" --merge-output-format mp4 \
    --concurrent-fragments 4 --retries 10 --fragment-retries 10 \
    --download-archive archive.txt \
    -o "%(playlist_index)03d_%(title)s.%(ext)s" "URL"
  # Then rename to romanized lowercase with underscores
  # e.g. 「機械オンチに「API」を説明する動画」→ kikai_onchi_ni_api_wo_setsumei_suru_douga_01.mp4
  ```
- **subs2cia**: Any step that uses subs2cia must use [snoozxy's fork](https://github.com/snoozxy/subs2cia). Install with: `pip install git+https://github.com/snoozxy/subs2cia.git`
- **Transcription provider**: Any skill that transcribes audio/video (create-srt, find-mistakes, style-guide) supports two providers — **ElevenLabs Scribe v2** and **Soniox**. Ask the user which to use each time, then run `dojo-prompts/scripts/transcribe.py --provider <elevenlabs|soniox>`. Both write the same canonical transcript JSON, so all downstream steps are identical. Make sure the chosen provider's key is set first — `$ELEVENLABS_API_KEY` or `$SONIOX_API_KEY`; if not, ask the user to paste it before transcribing.
- **jimaku.cc subtitles**: The `process-local` skill downloads Japanese subtitles from jimaku.cc. Requires `$JIMAKU_API_KEY` (generate at jimaku.cc/account). Use `dojo-prompts/scripts/jimaku_dl.py` for all API calls — search, file listing, and download.
- **Primed Listening**: `dojo-prompts/primed-listening.lua` is an mpv script, not an AI skill. To install it, copy it to `~/.config/mpv/scripts/`.
