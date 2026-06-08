---
name: process-local
description: |
  Process locally stored video files (downloaded anime, movies, dramas) into
  study materials. Downloads Japanese subtitles from jimaku.cc, syncs them to
  the video automatically using ffsubsync, and generates subs2cia Anki decks
  and/or condensed audio. Falls back to AI transcription if no good subtitle
  match is found.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
---

# Process Local Content

Process locally stored video files into Japanese study materials. Works for
anime, movies, and dramas — anything you have downloaded to your PC.

## Usage

Run `/process-local` and the skill will walk you through the process.

## Requirements

- `ffsubsync` — `pip install ffsubsync`
- `JIMAKU_API_KEY` — generate at [jimaku.cc/account](https://jimaku.cc/account)
- subs2cia, ffmpeg, and the other standard dojo dependencies

## Folder conventions

Establish these two variables before doing anything else. Every path in the workflow is derived from them.

**`PREFIX`** — a short, lowercase ASCII slug for this run. Use the show name with no spaces: `hunterxhunter`, `oshi_no_ko_s2`. For a playlist where you're processing a specific episode, append the number: `minecraft_letsplay_31`. This slug appears in every filename and folder so nothing gets mixed up across projects.

**`VIDEO_DIR`** — the folder containing the source video files. This may be on a separate drive. All processing happens here so large I/O stays local to the drive.

**`DOJO_DIR`** — `C:/Users/snoozy/Desktop/dojo` (always).

**Layout:**

```
VIDEO_DIR/
  480p/                            ← transcoded copies (optional, kept until deck verified)
  DOJO_TEMP/
    {PREFIX}/
      subs_download/               ← jimaku subtitle candidates
      synced_subs/                 ← synced SRT files
      out_srs/                     ← subs2cia audio clips, screenshots, per-episode TSVs
      combined.tsv                 ← merged TSV (all episodes)
      {PREFIX}_ep01.json           ← AI transcriptions (expensive — kept until archived)
      {PREFIX}.apkg                ← final deck (before archiving)

DOJO_DIR/archive/{PREFIX}/        ← permanent archive (created at the end)
  {PREFIX}.apkg
  combined.tsv
  {PREFIX}_ep01.json               ← transcriptions moved here
```

**Rules:**
- All intermediate work goes under `VIDEO_DIR/DOJO_TEMP/{PREFIX}/` — one place, one prefix
- Do NOT delete `DOJO_TEMP/{PREFIX}/` until the deck is imported into Anki and confirmed good — it contains expensive files (transcriptions, subs2cia output) that are slow to regenerate
- After the deck is verified: copy `{PREFIX}.apkg`, `combined.tsv`, and any `*.json` files to `DOJO_DIR/archive/{PREFIX}/`, then `rm -rf VIDEO_DIR/DOJO_TEMP/{PREFIX}/`

Set these at the start of every session:

```bash
PREFIX="hunterxhunter"               # ← change this
VIDEO_DIR="/d/anime/hunterxhunter"   # ← change this
DOJO_DIR="C:/Users/snoozy/Desktop/dojo"
TEMP="$VIDEO_DIR/DOJO_TEMP/$PREFIX"
mkdir -p "$TEMP/subs_download" "$TEMP/synced_subs" "$TEMP/out_srs"
```

## Workflow

### 1. Gather inputs up front

Ask the user for:

1. **Path** — a single video file, or a folder of videos for a series
2. **Prefix** — a short lowercase slug for this run (e.g. `hunterxhunter`). Suggest one based on the show name; let the user confirm or change it.
3. **Outputs** — what they want generated:
   - **Anki deck** — flashcards with audio clips and screenshots
   - **Condensed audio** — spoken audio only, for passive listening
   - **Japanese subtitles** — SRT for watching
   - **English subtitles** — translated SRT
4. **Video quality for screenshots** — for Anki decks, screenshots come from the
   source video. Ask: *"Use original resolution or transcode to 480p first?
   480p is much smaller and still readable in Anki."*
5. **Jimaku API key** — check `$JIMAKU_API_KEY`; if not set, ask the user to
   paste it and export it:
   ```bash
   export JIMAKU_API_KEY="<key>"
   ```

Wait for all answers, then set up the folder structure (see **Folder conventions** above) before doing anything else.

### 2. Prepare video files

**Single file:** use that file directly.

**Folder:** list all video files:
```bash
ls "$VIDEO_DIR"/*.mp4 "$VIDEO_DIR"/*.mkv "$VIDEO_DIR"/*.avi 2>/dev/null | sort
```

If filenames contain Japanese/CJK characters or are otherwise not ASCII-safe,
rename them to romanized lowercase with underscores before any processing
(same rules as `process-content.md`).

**480p transcode** (if user chose 480p for screenshots):
```bash
# First time on a new machine: check for GPU and get recommended flags
python3 dojo-prompts/scripts/hw_probe.py

# Then transcode (--encoder auto detects GPU if available)
python3 dojo-prompts/scripts/transcode_batch.py "$VIDEO_DIR" "$VIDEO_DIR/480p" \
  --height 480 --encoder auto
```
This transcodes all videos in parallel. GPU encoding (NVENC/AMF/QuickSync) is
5–10× faster than CPU for large batches or long files. Use the transcoded
versions for subs2cia only — keep the originals.

### 3. Search jimaku.cc for subtitles

Use the show title (ask the user if it's not clear from the filename):
```bash
python3 dojo-prompts/scripts/jimaku_dl.py search "Show Name"
# For live action / dramas, add --all:
python3 dojo-prompts/scripts/jimaku_dl.py search "Show Name" --all
```

Show the results to the user and ask them to confirm which entry to use.
If the search returns no results, try alternative spellings (English name,
Japanese name, romanized name).

### 4. Download subtitle files

Download all subtitle files for the entry into the temp directory:
```bash
python3 dojo-prompts/scripts/jimaku_dl.py download <entry_id> --out "$TEMP/subs_download"
```

This downloads all files (extracting from zips automatically). You'll end up
with a collection of `.srt` and/or `.ass` files from various fansub groups.

**For large series**, if there are many files you can filter by episode:
```bash
python3 dojo-prompts/scripts/jimaku_dl.py download <entry_id> --episode 1 --out "$TEMP/subs_download"
```

### 5. Match and sync subtitles

Use `sync_subs.py` to test all candidates in parallel and automatically pick
the best match:

```bash
# Test all candidates for episode 1, write best match to synced_subs/
python3 dojo-prompts/scripts/sync_subs.py \
  "video_01.mkv" "$TEMP/subs_download/" \
  -o "$TEMP/synced_subs/${PREFIX}_ep01.srt" --episode 1
```

The script runs ffsubsync on every candidate concurrently, prints a ranked
table of results, and copies the best match to the output path.

**Dual-audio files (important):** Many encodes (e.g. EMBER, dual-audio WEBRips)
have English as audio stream 0 and Japanese as stream 1. ffsubsync defaults to
stream 0, so syncing Japanese subtitles against English audio produces wildly
wrong offsets (often 25–60 s off). `sync_subs.py` auto-detects this via
ffprobe and passes `--reference-stream a:N` to ffsubsync automatically — no
extra flags needed. If auto-detection fails, override manually:
```bash
python3 dojo-prompts/scripts/sync_subs.py video.mkv subs/ -o out.srt \
  --reference-stream a:1
```

**Decision tree (applied automatically):**

| Abs offset | Result |
|---|---|
| ≤ 2s | **Perfect match** |
| 2s – 30s | **Good sync** |
| 30s – 300s | **Questionable** — used only if nothing better |
| > 300s or error | **Discarded** |

**For a folder of videos**, test against episode 1 first to identify the best
subtitle group. Then use `--episode N` for each subsequent episode — most
series use the same group throughout, so the same source that matched episode 1
will usually work for the rest. Confirm episode-by-episode only if offsets vary
significantly.

If `sync_subs.py` exits with code 1 (no candidate passed the threshold), fall
back to AI transcription for that episode (see step 6).

### 6. Fallback: AI transcription

If no subtitle candidate passes the 300s threshold for a video, fall back to AI
transcription for that video. Ask the user which provider to use:

```bash
python3 dojo-prompts/scripts/transcribe.py --provider <elevenlabs|soniox> \
  --language ja -o "$TEMP/${PREFIX}_ep01" video.mp4
```

Output JSON lands in `$TEMP/` (e.g. `$TEMP/hunterxhunter_ep01.json`). These
files are expensive to regenerate — they stay in DOJO_TEMP until the deck is
verified, then get moved to the archive alongside the apkg. Treat the JSON the
same as a synced SRT for downstream processing.

### 7. Generate outputs

Use the synced subtitle files (or transcript JSON from AI fallback) as input
to the requested output skills. Read the relevant skill files for full
instructions:

**Anki deck** — read `anki.md`. Before running subs2cia, always identify the Japanese audio stream with ffprobe — never assume stream 0 is Japanese:
```bash
# Always run this first
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "video.mkv"
```
Use the resulting Japanese stream index as `-ai`. Also check whether the subtitle file is pure Japanese (see the "Multi-language subtitle files" section in `anki.md`) — dual-language ASS files from fansubs will produce mixed-language cards.

```bash
# Single episode — output to TEMP/out_srs
PYTHONUTF8=1 subs2cia srs -i "video.mkv" "$TEMP/synced_subs/${PREFIX}_ep01.srt" \
  -ai <jp_audio_index> -p 500 -N -d "$TEMP/out_srs" --export-header-row

# Multi-episode batch
PYTHONUTF8=1 subs2cia srs -b -j 4 -i "$VIDEO_DIR/480p"/*.mkv \
  -ai <jp_audio_index> -p 500 -N -d "$TEMP/out_srs" --export-header-row

# With JSON (from AI transcription fallback)
PYTHONUTF8=1 subs2cia srs -i "video.mp4" "$TEMP/${PREFIX}_ep01.json" \
  -ai <jp_audio_index> -p 500 -N -d "$TEMP/out_srs" --export-header-row
```

`srs` clips audio directly from the source container — no FLAC demux step. Run `hw_probe.py` once so `SUBS2CIA_WORKERS`, `SUBS2CIA_JOBS`, and `SUBS2CIA_HWACCEL` are cached.
Then follow the full anki.md workflow (episode summaries → combine TSVs → export `.apkg` to `$TEMP/${PREFIX}.apkg`).

**Condensed audio** — read `condensed-audio.md`. Same ffprobe check applies — always identify the Japanese audio stream first and pass `-ai <jp_index>`. For multiple files, add `-b -j N`:
```bash
# With synced SRT:
PYTHONUTF8=1 subs2cia condense -i "video.mp4" "$TEMP/synced_subs/${PREFIX}_ep01.srt" \
  -ai <jp_audio_index> -t 1500 -p 200 --no-gen-subtitle -d "$TEMP/out_condense"

# Multi-episode batch:
PYTHONUTF8=1 subs2cia condense -b -j 4 -i "$VIDEO_DIR"/*.mp4 \
  -ai <jp_audio_index> -t 1500 -p 200 --no-gen-subtitle -d "$TEMP/out_condense"

# With JSON:
PYTHONUTF8=1 subs2cia condense -i "video.mp4" "$TEMP/${PREFIX}_ep01.json" \
  -ai <jp_audio_index> -t 1500 -p 200 --no-gen-subtitle -d "$TEMP/out_condense"
```

**Japanese subtitles** — if the synced SRT is already clean Japanese, use it
directly. If from AI transcription, run `srt_watch.py` on the JSON:
```bash
python3 dojo-prompts/scripts/srt_watch.py -o <video_stem> <video_stem>.json
```

**English subtitles** — read `translate-srt.md`. Works from either the synced
SRT or the AI transcript JSON.

### 8. Archive and clean up

Only do this after the user has imported the deck into Anki and confirmed it looks good.

```bash
# 1. Create the archive folder
mkdir -p "$DOJO_DIR/archive/$PREFIX"

# 2. Copy the permanent outputs
cp "$TEMP/$PREFIX.apkg"   "$DOJO_DIR/archive/$PREFIX/"
cp "$TEMP/combined.tsv"   "$DOJO_DIR/archive/$PREFIX/"
cp "$TEMP/"*.json         "$DOJO_DIR/archive/$PREFIX/" 2>/dev/null || true  # AI transcriptions

# 3. Delete the temp folder — all intermediate work gone
rm -rf "$VIDEO_DIR/DOJO_TEMP/$PREFIX"
```

The `480p/` transcode folder in `VIDEO_DIR` can also be deleted at this point if disk space is a concern — it can always be regenerated from the originals.

### 9. Report results

Tell the user:
- Which subtitle source was used for each video (fansub group + offset, or AI)
- Where the archive was written (`DOJO_DIR/archive/PREFIX/`)
- Confirmation that `DOJO_TEMP/PREFIX/` has been removed

## Notes

- **Episode number matching**: when a series download contains many files,
  episode numbers in filenames usually follow patterns like `- 01`, `E01`,
  `[01]`, `_01`. ffsubsync handles timing drift, so a subtitle file for the
  right episode will almost always sync correctly even if the timing source
  differs (BD vs WEB, etc.).

- **Hardcoded subtitles (PGS/bitmap)**: subs2cia can only work with text
  subtitles (SRT, ASS). If the video has only PGS/bitmap subs embedded, you
  must use AI transcription — there's no way to extract text from image subs.
  Check with:
  ```bash
  ffprobe -v error -select_streams s -show_entries stream=codec_name:stream_tags=language \
    -of csv=p=0 "video.mp4"
  ```
  If the codec is `hdmv_pgs_subtitle` or `dvd_subtitle`, AI transcription is needed.

- **Encoding issues**: some older fansub SRT files use Shift-JIS encoding.
  ffsubsync handles this, but if you see garbled characters, convert first:
  ```bash
  iconv -f SHIFT_JIS -t UTF-8 input.srt > input_utf8.srt
  ```

- **Rate limit**: jimaku.cc allows 25 API requests/minute. The downloader
  respects this automatically for normal use — don't loop it in a tight batch.
