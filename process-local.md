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

## Workflow

### 1. Gather inputs up front

Ask the user for:

1. **Path** — a single video file, or a folder of videos for a series
2. **Outputs** — what they want generated:
   - **Anki deck** — flashcards with audio clips and screenshots
   - **Condensed audio** — spoken audio only, for passive listening
   - **Japanese subtitles** — SRT for watching
   - **English subtitles** — translated SRT
3. **Video quality for screenshots** — for Anki decks, screenshots come from the
   source video. Ask: *"Use original resolution or transcode to 480p first?
   480p is much smaller and still readable in Anki."*
4. **Jimaku API key** — check `$JIMAKU_API_KEY`; if not set, ask the user to
   paste it and export it:
   ```bash
   export JIMAKU_API_KEY="<key>"
   ```

Wait for all answers before doing anything.

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
python3 dojo-prompts/scripts/transcode_batch.py "$VIDEO_DIR" "$VIDEO_DIR/480p" --height 480
```
This transcodes all videos in parallel (2 workers by default). Use the
transcoded versions for subs2cia only — keep the originals.

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

Download all subtitle files for the entry into a temp directory:
```bash
mkdir -p subs_download
python3 dojo-prompts/scripts/jimaku_dl.py download <entry_id> --out subs_download
```

This downloads all files (extracting from zips automatically). You'll end up
with a collection of `.srt` and/or `.ass` files from various fansub groups.

**For large series**, if there are many files you can filter by episode:
```bash
python3 dojo-prompts/scripts/jimaku_dl.py download <entry_id> --episode 1 --out subs_download
```

### 5. Match and sync subtitles

Use `sync_subs.py` to test all candidates in parallel and automatically pick
the best match:

```bash
# Test all candidates for episode 1, write best match to synced_subs/
python3 dojo-prompts/scripts/sync_subs.py \
  "video_01.mkv" subs_download/ \
  -o synced_subs/video_01.srt --episode 1
```

The script runs ffsubsync on every candidate concurrently, prints a ranked
table of results, and copies the best match to the output path.

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
  --language ja -o <video_stem> video.mp4
```

This produces a transcript JSON that feeds directly into the output steps below.
Treat it the same as a synced SRT for downstream processing.

### 7. Generate outputs

Use the synced subtitle files (or transcript JSON from AI fallback) as input
to the requested output skills. Read the relevant skill files for full
instructions:

**Anki deck** — read `anki.md`. Pass the video and synced SRT to subs2cia:
```bash
# With synced SRT (from subtitle match):
subs2cia srs -i "video.mp4" "video.synced.srt" -p 500 -N -d out_srs --export-header-row

# With JSON (from AI transcription fallback):
subs2cia srs -i "video.mp4" "video.json" -p 500 -N -d out_srs --export-header-row
```
Then follow the full anki.md workflow (episode summaries → combine TSVs → export .apkg).

**Condensed audio** — read `condensed-audio.md`. Pass the video and synced SRT:
```bash
# With synced SRT:
subs2cia condense -i "video.mp4" "video.synced.srt" -t 1500 -p 200 --no-gen-subtitle -d out_condense

# With JSON:
subs2cia condense -i "video.mp4" "video.json" -t 1500 -p 200 --no-gen-subtitle -d out_condense
```

**Japanese subtitles** — if the synced SRT is already clean Japanese, use it
directly. If from AI transcription, run `srt_watch.py` on the JSON:
```bash
python3 dojo-prompts/scripts/srt_watch.py -o <video_stem> <video_stem>.json
```

**English subtitles** — read `translate-srt.md`. Works from either the synced
SRT or the AI transcript JSON.

### 8. Clean up

Remove the temporary subtitle downloads and any intermediate files:
```bash
rm -rf subs_download/
rm -f *.synced.srt      # keep the originals
```

Keep: final Anki `.apkg`, condensed MP3s, final SRT files, and any transcript
JSON files (needed for other workflows).

### 9. Report results

Tell the user:
- Which subtitle source was used for each video (fansub group + offset, or AI)
- Where each output file is saved

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
