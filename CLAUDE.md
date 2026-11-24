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
  → [jimaku_dl.py]       download JP subtitle candidates from jimaku.cc
  → [sync_subs.py]       test all candidates in parallel, pick best sync
  → [clean_subs.py]      strip ASS tags, furigana, music symbols, HTML noise
  → [transcribe.py]      AI transcript — word-level timing + speaker labels
  → [fuse_subs.py]       fuse jimaku text onto AI timing/speakers → fused .json
  → [content2srs build]  audio clips + screenshots → combined .db bundle
  → [content2srs summary] attach episode briefings to the bundle
  → [content2srs export] package into final .apkg (or use --apkg on build)
```

**Why transcribe AND use jimaku subs?** Jimaku subs have accurate text (names, jargon, kanji the AI mishears) but only display-line timing and no speaker info — so cutting cards from them gives fragments (one cue) or run-ons (time-merged cues with multiple speakers jammed together). The AI transcript has the opposite: real per-character timing and `speaker_id`, but worse text. `fuse_subs.py` aligns the two and keeps the best of each — jimaku text on AI timing/speakers — so `content2srs` runs its normal Japanese sentence/speaker segmentation and produces cards that break on real sentence and speaker boundaries with accurate text. This replaces the old `--resegment` flag (which only interpolated timing within each cue and had no speaker awareness). If no jimaku match exists for an episode, build straight from the AI transcript `.json` (no fusion step).

**No pre-transcode step.** `content2srs` already downscales every screenshot to
1280px wide at extraction time regardless of source resolution (it's a fixed
`scale=1280:-1` in its ffmpeg frame-extraction filter), so building directly
from the original video files produces the same screenshot size as building
from a 480p proxy — without paying for a full re-encode pass first.
`transcode_batch.py` still exists for cases where disk space is tight, but
it's optional, not part of the default pipeline.

**Don't skip the episode briefing step.** It attaches a translation briefing to every card's context field via `content2srs summary` or the `--summaries` flag, making LLM-assisted study much more useful. Read `anki.md` for the briefing format.

Key commands (fill in actual paths). Set these variables first — every path derives from them:
```bash
PREFIX="hunterxhunter"               # short lowercase slug for this run
VIDEO_DIR="/d/anime/hunterxhunter"   # folder with the video files
DOJO_DIR="C:/Users/snoozy/Desktop/dojo"
CONTENT2SRS="$DOJO_DIR/content2srs/target/release/content2srs.exe"
TEMP="$VIDEO_DIR/DOJO_TEMP/$PREFIX"
mkdir -p "$TEMP/subs_download" "$TEMP/synced_subs"
```

```bash
# 1. Hardware check (once — skip if ~/.dojo_hw_cache.json exists and is recent)
python3 dojo-prompts/scripts/hw_probe.py --check || python3 dojo-prompts/scripts/hw_probe.py

# 2. Search jimaku.cc for subtitles (requires $JIMAKU_API_KEY)
python3 dojo-prompts/scripts/jimaku_dl.py search "Show Name"
python3 dojo-prompts/scripts/jimaku_dl.py download <entry_id> --out "$TEMP/subs_download"

# 3. Sync subtitles (test all candidates, pick best)
python3 dojo-prompts/scripts/sync_subs.py "$VIDEO_DIR/show_01.mkv" "$TEMP/subs_download/" \
  -o "$TEMP/synced_subs/${PREFIX}_ep01.srt" --episode 1

# 3b. Clean synced subtitles (strip ASS tags, furigana, music symbols, HTML)
python3 dojo-prompts/scripts/clean_subs.py "$TEMP/synced_subs/"*.srt --inplace

# 4. Identify Japanese audio stream index — ALWAYS do this first.
# Many encodes put English audio at stream 0. Never assume 0 is Japanese.
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "$VIDEO_DIR/show_01.mkv"

# 5. Transcribe each episode (AI), then fuse the jimaku text onto its timing.
# transcribe.py auto-detects the Japanese audio stream; fuse_subs.py aligns the
# cleaned jimaku SRT onto the AI word timing + speaker_id and writes a fused .json.
# Route fused files to their own dir with plain .json names so they pair with the
# video by stem and don't collide with the .srt in synced_subs/.
mkdir -p "$TEMP/fused"
python3 dojo-prompts/scripts/transcribe.py --provider <elevenlabs|soniox> \
  --language ja -o "$TEMP/${PREFIX}_ep01" "$VIDEO_DIR/show_01.mkv"
python3 dojo-prompts/scripts/fuse_subs.py \
  "$TEMP/${PREFIX}_ep01.json" "$TEMP/synced_subs/${PREFIX}_ep01.srt" \
  -o "$TEMP/fused/show_01.json"      # stem must match the video (show_01.mkv)

# 6. Generate episode briefings JSON (read synced SRTs, write summaries.json)
# Format: { "sources": { "show_01.mkv": "Episode 1 briefing...", ... } }
# (see anki.md for the Episode Summary Format)

# 7. Build deck — all episodes in one pass (produces .db bundle + .apkg)
# Build directly from the original video files — content2srs downscales
# screenshots to 1280px on its own, so no pre-transcode is needed. No
# --resegment: the fused transcript is already sentence-segmented like any
# AI transcript.
"$CONTENT2SRS" build -b -j 4 \
  -i "$VIDEO_DIR/"*.mkv "$TEMP/fused/"*.json \
  --audio-index <jp_audio_index> --loudnorm -p 500 \
  --summaries "$TEMP/summaries.json" \
  --deck-name "$PREFIX" \
  -o "$TEMP/$PREFIX.db" --apkg "$TEMP/$PREFIX.apkg"

# 8. After verifying the deck in Anki — archive and clean up
mkdir -p "$DOJO_DIR/archive/$PREFIX"
cp "$TEMP/$PREFIX.apkg" "$DOJO_DIR/archive/$PREFIX/"
cp "$TEMP/"*.json "$DOJO_DIR/archive/$PREFIX/" 2>/dev/null || true
rm -rf "$VIDEO_DIR/DOJO_TEMP/$PREFIX"
```

## Scripts reference

All helper scripts live in `dojo-prompts/scripts/`:

| Script | What it does |
|---|---|
| `hw_probe.py` | Detect CPU/RAM/GPU, test ffmpeg encoders, cache to `~/.dojo_hw_cache.json`. Run once per machine. |
| `transcode_batch.py` | Parallel ffmpeg transcode with GPU auto-detection (`--encoder auto`). Optional — not part of the default Anki pipeline (content2srs downscales screenshots itself); use only to shrink files for disk space. |
| `jimaku_dl.py` | Search, list files, and download subtitles from jimaku.cc. |
| `sync_subs.py` | Test subtitle candidates against a video in parallel, copy best match. `--group SUBSTR` narrows candidates to one fansub group by filename substring — use for episodes 2+ once episode 1 has identified the winner, so only one ffsubsync job runs instead of testing every group again. With `--fuse --provider <p>` it chains clean→transcribe→fuse after syncing, producing the fused `.json` in one command (reuses an existing `--transcript` so transcription never runs twice). Independent per episode — safe to background several at once (`&` + `wait`) once the winning group is known. |
| `clean_subs.py` | Strip ASS override tags, HTML, ruby/furigana annotations, music symbols from SRT/ASS files. ASS input is converted to SRT. Run after sync, before fuse/content2srs. `--inplace` overwrites SRT files in place. |
| `fuse_subs.py` | Fuse a cleaned jimaku SRT with an AI transcript JSON → a fused transcript `.json` (accurate jimaku text on AI per-character timing + speaker labels). Drop-in for the content2srs JSON path and `srt_watch.py`/`srt_translate.py`. Run after `clean_subs.py` + `transcribe.py`, before `content2srs build`. |
| `transcribe.py` | Transcribe audio/video via ElevenLabs Scribe or Soniox. Resumes after crashes. Audio file is a **positional argument** (not `--input`). `-o` can be passed with or without `.json` — the script always appends `.json` to the stem. The API call is pure network I/O and per-episode state is fully isolated, so it's safe (and much faster wall-clock) to background 3–4 episodes' transcriptions at once instead of running them sequentially. |

Anki deck creation uses the `content2srs` binary at `content2srs/target/release/content2srs.exe` — see `anki.md`.

## Windows notes

On Windows, `subs2cia` and `ffsubsync` are installed in the Python user Scripts directory and are **not on the Git Bash PATH**. Use the full executable path:

```bash
SUBS2CIA="C:/Users/snoozy/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/Scripts/subs2cia.exe"
FFSUBSYNC="C:/Users/snoozy/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/Scripts/ffsubsync.exe"
```

`content2srs` is a native Rust binary — set it once per session:
```bash
CONTENT2SRS="C:/Users/snoozy/Desktop/dojo/content2srs/target/release/content2srs.exe"
```

`sync_subs.py` finds `ffsubsync.exe` automatically using this path — no manual path needed when using the script. Always prefix `subs2cia` commands with `PYTHONUTF8=1` to handle Japanese filenames. `content2srs` handles Unicode natively — no prefix needed.

**YouTube title lookup on Windows** — `yt-dlp --print "%(title)s"` silently garbles or drops Japanese characters (cp1252 shell encoding). Use JSON dump piped through Python with explicit utf-8 output instead:

```bash
# Get the full title for any video
PYTHONUTF8=1 yt-dlp --no-download -J "URL" | python3 -c "
import sys, json, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
print(json.load(sys.stdin).get('title', ''))
"

# Get playlist item IDs (no titles — those garble; resolve per-video above)
yt-dlp --flat-playlist --print "%(playlist_index)s|%(id)s" "PLAYLIST_URL"
```

To download specific episodes by position: `--playlist-items 19,20` (1-based, matches `playlist_index`).

**content2srs audio-index for YouTube MP4s** — YouTube MP4s have video at global stream 0 and audio at stream 1. Don't pass `--audio-index` for single-audio YouTube files; content2srs auto-detects correctly. Forcing `--audio-index 0` hits the video stream and errors with "no audio stream found".

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
| `SUBS2CIA_HWACCEL` | GPU decode for condensed-audio screenshot seeks |
| `SUBS2CIA_WORKERS` | Per-episode card export parallelism (condensed-audio only) |
| `SUBS2CIA_JOBS` | Parallel episodes for condensed-audio batch (`-b -j`) |
| `FFMPEG_ENCODER` / `FFMPEG_HWACCEL` | Used by `transcode_batch.py` |

`content2srs` auto-detects hardware and worker count — no hw_probe cache needed. Set `CONTENT2SRS_JOBS=N` (env var) to override parallel episode count in batch builds.

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

**content2srs binary:** Check it exists:
```bash
test -f "C:/Users/snoozy/Desktop/dojo/content2srs/target/release/content2srs.exe" \
  || echo "MISSING: content2srs — build with: cd content2srs && cargo build --release"
```

**subs2cia** (needed for condensed-audio only): Even if installed, verify it's the correct fork:
```bash
pip show subs2cia 2>/dev/null | grep -i "home-page\|location"
```
If NOT from `github.com/snoozxy/subs2cia`, reinstall the correct fork:
```bash
pip uninstall -y subs2cia
pip install git+https://github.com/snoozxy/subs2cia.git
```
If it IS the correct fork, upgrade it:
```bash
pip install --upgrade git+https://github.com/snoozxy/subs2cia.git
```

If a required tool is missing, just install it and move on. No need to ask — but don't reinstall things that are already there.

## Important

- **Downloading videos**: Always use yt-dlp and always download as MP4. **Always ask the user whether they want 480p (default — smaller files, fine for Anki/audio work) or best available quality (original resolution, much larger) before downloading.** After downloading, rename files with a romanized version of the full title (see `process-content.md` for detailed naming rules):
  ```bash
  # Format strings — pick one based on user's quality choice:
  FORMAT_480P="bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]"
  FORMAT_BEST="bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]"

  # Single video (replace $FORMAT with chosen format string above)
  yt-dlp -f "$FORMAT" --merge-output-format mp4 \
    --concurrent-fragments 4 --retries 10 --fragment-retries 10 --no-playlist \
    -o "%(title)s.%(ext)s" "URL"
  # Playlist or channel
  yt-dlp -f "$FORMAT" --merge-output-format mp4 \
    --concurrent-fragments 4 --retries 10 --fragment-retries 10 \
    --download-archive archive.txt \
    -o "%(playlist_index)03d_%(title)s.%(ext)s" "URL"
  # Then rename to romanized lowercase with underscores
  # e.g. 「機械オンチに「API」を説明する動画」→ kikai_onchi_ni_api_wo_setsumei_suru_douga_01.mp4
  ```
- **content2srs**: Anki deck creation uses the `content2srs` Rust binary in this repo. Build with: `cd content2srs && cargo build --release`. Binary lives at `content2srs/target/release/content2srs.exe`.
- **subs2cia**: Still required for the condensed-audio skill only. Must use [snoozxy's fork](https://github.com/snoozxy/subs2cia). Install with: `pip install git+https://github.com/snoozxy/subs2cia.git`
- **Transcription provider**: Any skill that transcribes audio/video (create-srt, find-mistakes, style-guide) supports two providers — **ElevenLabs Scribe v2** and **Soniox**. Ask the user which to use each time. Both write the same canonical transcript JSON, so all downstream steps are identical. Make sure the chosen provider's key is set first — `$ELEVENLABS_API_KEY` or `$SONIOX_API_KEY`; if not, ask the user to paste it before transcribing.

  Correct invocation — **audio file is a positional argument, not `--input`**:
  ```bash
  ELEVENLABS_API_KEY=<key> python3 dojo-prompts/scripts/transcribe.py \
    --provider elevenlabs --language ja \
    -o DOJO_TEMP/prefix/prefix.json \
    path/to/video.mp4
  ```
  The `-o` path may include `.json` or omit it — the script normalizes either form. Output always lands at `<stem>.json`.

  **Soniox 5-hour cap:** Soniox rejects any single file longer than 18,000 s (5 hours) with `invalid_audio_file` ("Maximum audio duration exceeded"). The `.mka`/`.mp3` container is *not* the problem — it's purely duration. `transcribe.py` now handles this automatically: it probes duration and, if over the cap, splits the audio into even sub-cap mp3 chunks, transcribes each, and merges them into one JSON with per-chunk timestamp offsets. No manual splitting needed. (ElevenLabs has no such duration cap.)

- **content2srs build speed**: On this machine (d3d11va GPU decode, 8 workers) content2srs is much faster than the estimates in older notes — a ~3000-card episode completes in ~90 seconds, not 15-20 minutes. Expect roughly 1 minute per 2000 cards for YouTube MP4 source material. Audio clip extraction batches ~96 clips per ffmpeg invocation (via an `asplit`/`atrim` filter graph, mirroring the screenshot batching in `frames.rs`) instead of spawning one ffmpeg process per card — verified ~3x faster on a dense 2600-card synthetic benchmark (34s → 11s) with correct output even for overlapping padded ranges.
- **jimaku.cc subtitles**: The `process-local` skill downloads Japanese subtitles from jimaku.cc. Requires `$JIMAKU_API_KEY` (generate at jimaku.cc/account). Use `dojo-prompts/scripts/jimaku_dl.py` for all API calls — search, file listing, and download.
- **Primed Listening**: `dojo-prompts/primed-listening.lua` is an mpv script, not an AI skill. To install it, copy it to `~/.config/mpv/scripts/`.
