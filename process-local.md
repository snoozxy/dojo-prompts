---
name: process-local
description: |
  Process locally stored video files (downloaded anime, movies, dramas) into
  study materials. Downloads Japanese subtitles from jimaku.cc, syncs them to
  the video automatically using ffsubsync, and generates content2srs Anki decks
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
- content2srs binary, ffmpeg, and the other standard dojo dependencies

## Folder conventions

Establish these two variables before doing anything else. Every path in the workflow is derived from them.

**`PREFIX`** — a short, lowercase ASCII slug for this run. Use the show name with no spaces: `hunterxhunter`, `oshi_no_ko_s2`. For a playlist where you're processing a specific episode, append the number: `minecraft_letsplay_31`. This slug appears in every filename and folder so nothing gets mixed up across projects.

**`VIDEO_DIR`** — the folder containing the source video files. This may be on a separate drive. All processing happens here so large I/O stays local to the drive.

**`DOJO_DIR`** — `C:/Users/snoozy/Desktop/dojo` (always).

**Layout:**

```
VIDEO_DIR/
  DOJO_TEMP/
    {PREFIX}/
      subs_download/               ← jimaku subtitle candidates
      synced_subs/                 ← synced + cleaned SRT files
      fused/                       ← fused transcripts (jimaku text + AI timing), .json per ep
      summaries.json               ← episode briefings (generated before content2srs build)
      {PREFIX}.db                  ← content2srs bundle (audio/image blobs + cards)
      {PREFIX}_ep01.json           ← AI transcriptions (expensive — kept until archived)
      {PREFIX}.apkg                ← final deck (before archiving)

DOJO_DIR/archive/{PREFIX}/        ← permanent archive (created at the end)
  {PREFIX}.apkg
  {PREFIX}_ep01.json               ← transcriptions moved here
```

**Rules:**
- All intermediate work goes under `VIDEO_DIR/DOJO_TEMP/{PREFIX}/` — one place, one prefix
- Do NOT delete `DOJO_TEMP/{PREFIX}/` until the deck is imported into Anki and confirmed good — it contains expensive files (transcriptions, content2srs bundle) that are slow to regenerate
- After the deck is verified: copy `{PREFIX}.apkg` and any `*.json` files to `DOJO_DIR/archive/{PREFIX}/`, then `rm -rf VIDEO_DIR/DOJO_TEMP/{PREFIX}/`

Set these at the start of every session:

```bash
PREFIX="hunterxhunter"               # ← change this
VIDEO_DIR="/d/anime/hunterxhunter"   # ← change this
DOJO_DIR="C:/Users/snoozy/Desktop/dojo"
CONTENT2SRS="$DOJO_DIR/content2srs/target/release/content2srs.exe"
TEMP="$VIDEO_DIR/DOJO_TEMP/$PREFIX"
mkdir -p "$TEMP/subs_download" "$TEMP/synced_subs"
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
4. **Jimaku API key** — check `$JIMAKU_API_KEY`; if not set, ask the user to
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

No transcode step is needed — `content2srs` already downscales every
screenshot to 1280px wide at extraction time, regardless of source
resolution, so building directly from the original files produces the same
screenshot size as a 480p proxy would, without the cost of a full re-encode
pass. If disk space is tight on a slow drive, `transcode_batch.py` is still
available to shrink files before processing (see Scripts reference), but it's
optional and no longer part of the default workflow.

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

After syncing all episodes, clean the output SRTs before passing to content2srs:

```bash
python3 dojo-prompts/scripts/clean_subs.py "$TEMP/synced_subs/"*.srt --inplace
```

This strips ASS override tags, HTML formatting, ruby/furigana annotations
(`｜word《reading》`), kana-only parenthetical readings, and music symbols (♪).
Empty cues (e.g. lines that were only ♪♪♪) are dropped. Works on both SRT and
ASS files; ASS is converted to clean SRT automatically.

**Dual-audio files (important):** Many encodes (e.g. EMBER, dual-audio WEBRips)
have English as audio stream 0 and Japanese as stream 1. ffsubsync defaults to
stream 0, so syncing Japanese subtitles against English audio produces wildly
wrong offsets (often 25–60 s off). `sync_subs.py` auto-detects this via
ffprobe and passes `--reference-stream a:N` to ffsubsync automatically — no
extra flags needed.

**Decision tree (applied automatically):**

| Abs offset | Result |
|---|---|
| ≤ 2s | **Perfect match** |
| 2s – 30s | **Good sync** |
| 30s – 300s | **Questionable** — used only if nothing better |
| > 300s or error | **Discarded** |

**For a folder of videos**, test against episode 1 first to identify the best
subtitle group. Once you know the winner (e.g. from the filename `[SubsPlease]`
or a fansub group tag), pass `--group "<substring>"` for every subsequent
episode so `sync_subs.py` only tests that one group's file instead of
re-running ffsubsync against every candidate for every episode:

```bash
python3 dojo-prompts/scripts/sync_subs.py \
  "video_02.mkv" "$TEMP/subs_download/" \
  -o "$TEMP/synced_subs/${PREFIX}_ep02.srt" --episode 2 --group "SubsPlease"
```

`--group` does a case-insensitive substring match on the filename; if nothing
matches it falls back to testing all candidates for that episode with a
warning, so it's safe to use speculatively.

**Parallelize across episodes.** Once the winning group is known, each
episode's sync is now a single independent ffsubsync job — run several
episodes' `sync_subs.py` at once in the background instead of waiting for each
to finish sequentially:

```bash
for n in 02 03 04 05; do
  python3 dojo-prompts/scripts/sync_subs.py \
    "video_${n}.mkv" "$TEMP/subs_download/" \
    -o "$TEMP/synced_subs/${PREFIX}_ep${n}.srt" --episode "${n#0}" --group "SubsPlease" &
done
wait
```
Keep the background batch size modest (4–6 at a time) to avoid saturating disk
I/O, since each job still reads the full video file.

If `sync_subs.py` exits with code 1 (no candidate passed the threshold), fall
back to AI transcription for that episode (see step 6).

### 6. Transcribe for timing & speakers, then fuse

Jimaku subs have accurate text but only display-line timing and no speaker info, so
cutting cards straight from them gives fragments (one cue per card) or run-ons
(time-merged cues with multiple speakers in one card). The fix is to transcribe the
audio with AI — purely for its real per-character timing and `speaker_id` — then fuse
the accurate jimaku text onto that timing. So for **Anki decks, transcription is a
standard step, not just a fallback.**

Ask the user which provider to use, then transcribe each episode (transcribe.py
auto-detects the Japanese audio stream):

```bash
python3 dojo-prompts/scripts/transcribe.py --provider <elevenlabs|soniox> \
  --language ja -o "$TEMP/${PREFIX}_ep01" "$VIDEO_DIR/show_01.mkv"
```

Output JSON lands in `$TEMP/` (e.g. `$TEMP/hunterxhunter_ep01.json`). These files are
expensive to regenerate — they stay in DOJO_TEMP until the deck is verified, then get
moved to the archive alongside the apkg.

**Parallelize transcription across episodes.** The API call is pure network I/O
(upload + poll), and each episode's state is fully isolated (separate
`.transcription_state.json`, separate audio sidecar per video), so transcribing
several episodes concurrently is safe — it's currently the slowest step in the
pipeline and is embarrassingly parallel across episodes. Run a handful in the
background at once instead of waiting for each to finish:

```bash
for n in 01 02 03 04; do
  python3 dojo-prompts/scripts/transcribe.py --provider <elevenlabs|soniox> \
    --language ja -o "$TEMP/${PREFIX}_ep${n}" "$VIDEO_DIR/show_${n}.mkv" &
done
wait
```
Start with 3–4 concurrent jobs and back off if the provider starts rate-limiting
or erroring — neither ElevenLabs nor Soniox document a hard concurrency limit
here, so this hasn't been stress-tested at higher counts.

**Fuse** the cleaned jimaku SRT onto the AI timing/speakers. Route fused files to their
own dir with plain `.json` names matching the **video** stem, so batch mode pairs them
correctly and they don't collide with the raw `.srt`:

```bash
mkdir -p "$TEMP/fused"
python3 dojo-prompts/scripts/fuse_subs.py \
  "$TEMP/${PREFIX}_ep01.json" "$TEMP/synced_subs/${PREFIX}_ep01.srt" \
  -o "$TEMP/fused/show_01.json"      # stem must match the video file (show_01.mkv)
```

`fuse_subs.py` reports the share of characters that got exact AI timing vs. interpolated
and an overall alignment ratio. A low ratio (warned at <0.5) usually means the transcript
and SRT are different episodes, or the SRT still needs `clean_subs.py`.

**One-command shortcut.** `sync_subs.py --fuse` runs sync + clean + transcribe + fuse in a
single call, so steps 5–6 collapse to one command per episode. An existing `--transcript`
is reused, so transcription never repeats:

```bash
python3 dojo-prompts/scripts/sync_subs.py \
  "$VIDEO_DIR/show_01.mkv" "$TEMP/subs_download/" --episode 1 \
  -o "$TEMP/synced_subs/${PREFIX}_ep01.srt" \
  --fuse --provider <elevenlabs|soniox> \
  --transcript "$TEMP/${PREFIX}_ep01.json" \
  --fused-out "$TEMP/fused/show_01.json"      # stem must match the video
```

**Parallelize episodes 2+ in one shot.** Once episode 1 has picked the winning
fansub group, each later episode's sync+transcribe+fuse chain is independent —
combine `--group` (skip re-testing every candidate) with backgrounding several
episodes at once, since transcription (the slow part) is pure network I/O:

```bash
for n in 02 03 04 05; do
  python3 dojo-prompts/scripts/sync_subs.py \
    "$VIDEO_DIR/show_${n}.mkv" "$TEMP/subs_download/" --episode "${n#0}" --group "SubsPlease" \
    -o "$TEMP/synced_subs/${PREFIX}_ep${n}.srt" \
    --fuse --provider <elevenlabs|soniox> \
    --transcript "$TEMP/${PREFIX}_ep${n}.json" \
    --fused-out "$TEMP/fused/show_${n}.json" &
done
wait
```
Start with 3–4 concurrent episodes; back off if ffsubsync/ffmpeg disk I/O or the
transcription provider starts to choke.

To **re-fuse an already-synced SRT** without re-running ffsubsync (e.g. you hand-edited
the SRT, or the chain failed partway), add `--no-sync` and drop the candidates dir — it
reuses the transcript, so no re-transcription either:

```bash
python3 dojo-prompts/scripts/sync_subs.py \
  "$VIDEO_DIR/show_01.mkv" -o "$TEMP/synced_subs/${PREFIX}_ep01.srt" --no-sync \
  --transcript "$TEMP/${PREFIX}_ep01.json" \
  --fused-out "$TEMP/fused/show_01.json"
```

**No jimaku match for an episode?** Skip the fuse step and build directly from that
episode's AI transcript `.json` — it's already sentence-segmented. (This is the case the
step 5 sync failure falls through to: transcribe, then use the transcript as-is.)

### 7. Generate outputs

Use the fused transcripts from step 6 (or the plain AI transcript JSON for any
no-jimaku episodes) as input.

**Anki deck** — Read `anki.md` for full instructions. Before running content2srs, always identify the Japanese audio stream with ffprobe:

```bash
# Always run this first
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "video.mkv"
```

Also check whether the subtitle file is pure Japanese (see the "Multi-language subtitle files" section in `anki.md`) — dual-language ASS files from fansubs will produce mixed-language cards.

**Generate episode briefings** before running content2srs. For each episode, read the subtitle text and generate a translation briefing (see Episode Summary Format in `anki.md`). Write all briefings to `$TEMP/summaries.json`:

```json
{
  "sources": {
    "show_ep01.mkv": "Episode 1 briefing...",
    "show_ep02.mkv": "Episode 2 briefing..."
  }
}
```

**Run content2srs** — build from the fused `.json` files (step 6); they pair with the
videos by stem. No `--resegment` — the fused transcript is already sentence-segmented
like any AI transcript, with real timing and speaker labels.

```bash
# Multi-episode batch (fused .json in TEMP/fused/, videos in VIDEO_DIR — no
# transcode needed; content2srs downscales screenshots to 1280px on its own)
"$CONTENT2SRS" build -b -j 4 \
  -i "$VIDEO_DIR/"*.mkv "$TEMP/fused/"*.json \
  --audio-index <jp_audio_index> --loudnorm -p 500 \
  --summaries "$TEMP/summaries.json" \
  --deck-name "$PREFIX" \
  -o "$TEMP/$PREFIX.db" --apkg "$TEMP/$PREFIX.apkg"

# Single episode
"$CONTENT2SRS" build \
  -i "$VIDEO_DIR/show_01.mkv" -s "$TEMP/fused/show_01.json" \
  --audio-index <jp_audio_index> --loudnorm -p 500 \
  --deck-name "$PREFIX" \
  -o "$TEMP/$PREFIX.db" --apkg "$TEMP/$PREFIX.apkg"

# No-jimaku episodes: build straight from the AI transcript .json (no fusion).
"$CONTENT2SRS" build \
  -i "video.mp4" -s "$TEMP/${PREFIX}_ep01.json" \
  --audio-index <jp_audio_index> --loudnorm -p 500 \
  --deck-name "$PREFIX" \
  -o "$TEMP/$PREFIX.db" --apkg "$TEMP/$PREFIX.apkg"
```

If summaries weren't passed via `--summaries`, add them after and re-export:
```bash
"$CONTENT2SRS" summary import -i "$TEMP/$PREFIX.db" -f "$TEMP/summaries.json"
"$CONTENT2SRS" export -i "$TEMP/$PREFIX.db" -o "$TEMP/$PREFIX.apkg" --deck-name "$PREFIX"
```

**Condensed audio** — read `condensed-audio.md`. Same ffprobe check applies. Uses subs2cia condense (not content2srs):
```bash
SUBS2CIA="C:/Users/snoozy/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/Scripts/subs2cia.exe"

# With synced SRT:
PYTHONUTF8=1 "$SUBS2CIA" condense -i "video.mp4" "$TEMP/synced_subs/${PREFIX}_ep01.srt" \
  -ai <jp_audio_index> -t 1500 -p 200 --no-gen-subtitle -d "$TEMP/out_condense"

# With JSON:
PYTHONUTF8=1 "$SUBS2CIA" condense -i "video.mp4" "$TEMP/${PREFIX}_ep01.json" \
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
cp "$TEMP/"*.json         "$DOJO_DIR/archive/$PREFIX/" 2>/dev/null || true  # AI transcriptions

# 3. Delete the temp folder — all intermediate work gone
rm -rf "$VIDEO_DIR/DOJO_TEMP/$PREFIX"
```

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

- **Subtitle pairing in batch mode**: content2srs pairs videos and subtitle files
  by filename stem — `ep01.mkv` pairs with `ep01.srt`. The synced subs in
  `$TEMP/synced_subs/` should have stems matching the video files in
  `VIDEO_DIR`. Name them accordingly when running sync_subs.py.

- **Hardcoded subtitles (PGS/bitmap)**: content2srs can only work with text
  subtitles (SRT, ASS, JSON). If the video has only PGS/bitmap subs embedded,
  you must use AI transcription. Check with:
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
