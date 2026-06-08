---
name: anki
description: |
  Create subs2srs Anki decks from video files using content2srs. Generates audio
  clips and subtitle text for flashcard-based language learning.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - AskUserQuestion
---

# Subs2SRS Deck Generator

Create subs2srs Anki decks from video files using content2srs. Generates audio clips and subtitle text for flashcard-based language learning.

## content2srs binary

All Anki deck work uses `content2srs` — a native Rust binary in this repo. Set the path once:

```bash
CONTENT2SRS="C:/Users/snoozy/Desktop/dojo/content2srs/target/release/content2srs.exe"
```

If the binary is missing or stale, rebuild it:
```bash
cd C:/Users/snoozy/Desktop/dojo/content2srs && cargo build --release
```

## Usage

The user provides a source directory containing video files (typically .mp4 with target language audio and subtitles). The skill processes all videos and outputs a single .apkg Anki deck. The user is an English speaker. The default target language is **Japanese**, but the user may specify another language.

## Track Selection

**Always run ffprobe before any content2srs command.** Never assume stream 0 is Japanese — many encodes (dual-audio WEBRips, EMBER, etc.) put English audio first. The wrong stream produces garbage cards.

```bash
# Check audio tracks
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "video.mkv"

# Check subtitle tracks (only needed for embedded subs)
ffprobe -v error -select_streams s \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "video.mkv"
```

Find the stream index where `language` is `jpn`, `ja`, `japanese`, or `日本語`. Use that index for `--audio-index`. If there are multiple Japanese tracks, ask the user which to use.

### Other Languages
Adapt track selection to whatever language the user specifies. Use the same ffprobe approach — match by language code and title tags.

### Multi-language subtitle files

Dual-language fansub ASS files and some jimaku downloads contain both Japanese and English lines in a single file. Passing these directly to content2srs will mix both languages into card text. Before using an external subtitle file, check whether it's pure Japanese:

```bash
# Rough check: count CJK characters vs pure-ASCII lines
python3 -c "
import re, sys
lines = open(sys.argv[1], encoding='utf-8', errors='replace').readlines()
# For ASS: only look at Dialogue lines
dial = [l for l in lines if l.startswith('Dialogue:')]
text_lines = dial if dial else lines
cjk = sum(1 for l in text_lines if re.search(r'[　-鿿＀-￯]', l))
latin = sum(1 for l in text_lines if re.search(r'[a-zA-Z]{4,}', l) and not re.search(r'[　-鿿]', l))
print(f'CJK lines: {cjk}, Latin-only lines: {latin}')
" subtitle.srt
```

If `latin-only lines` is significant (more than a handful of timing/metadata lines), the file is likely dual-language. Either find a Japanese-only file from the jimaku download set, or strip English lines from the ASS.

### Check Available Input Sources

Before running content2srs, check for input sources in priority order:

**1. transcript JSON files (preferred)** — Check if there are transcript JSON files alongside the videos:
```bash
ls "$SOURCE_DIR"/*.json 2>/dev/null
```
If JSON files exist, use them as input. content2srs uses UniDic-based sentence segmentation for Japanese — one card per sentence, better card boundaries than SRT splitting.

**2. External SRT/ASS files (fallback)** — Check if there are subtitle files alongside the videos:
```bash
ls "$SOURCE_DIR"/*.srt "$SOURCE_DIR"/*.ass 2>/dev/null
```
In batch mode, content2srs pairs subtitle files automatically by filename stem.

**3. Embedded tracks (last resort)** — Use `--subtitle-index` with the Japanese stream index from ffprobe.

## Base Command

```bash
CONTENT2SRS="C:/Users/snoozy/Desktop/dojo/content2srs/target/release/content2srs.exe"

# Single episode — external subtitle (sidecar .srt/.json matches video stem)
"$CONTENT2SRS" build \
  -i "video.mkv" -s "subtitle.srt" \
  --audio-index <jp_audio_index> \
  --loudnorm -p 500 \
  --deck-name "<show_name>" \
  -o deck.db --apkg deck.apkg

# Multi-episode batch — pass all videos and sidecar subs together; paired by stem
"$CONTENT2SRS" build -b -j 4 \
  -i "480p/"*.mkv "synced_subs/"*.srt \
  --audio-index <jp_audio_index> \
  --loudnorm -p 500 \
  --summaries summaries.json \
  --deck-name "<show_name>" \
  -o deck.db --apkg deck.apkg

# With embedded subtitle track (last resort)
"$CONTENT2SRS" build \
  -i "video.mkv" \
  --audio-index <jp_audio_index> --subtitle-index <jp_sub_index> \
  --loudnorm -p 500 \
  --deck-name "<show_name>" \
  -o deck.db --apkg deck.apkg
```

### Parameters Explained

| Flag | Value | Purpose |
|------|-------|---------|
| `build` | - | Build subcommand — extracts audio + screenshots, writes bundle |
| `-b` | - | Batch mode — discovers episodes from mixed video+subtitle inputs |
| `-j` | `4` | Parallel episodes in batch mode (env: `CONTENT2SRS_JOBS`) |
| `-i` | `*.mkv *.srt` | Input files (videos and/or sidecar subtitles) |
| `-s` | `subtitle.srt` | External subtitle file (single-episode mode only) |
| `--audio-index` | `0` | Japanese audio stream index (always required — check with ffprobe) |
| `--subtitle-index` | `0` | Embedded subtitle stream index (only for embedded subs) |
| `-p` | `500` | Padding in ms around each subtitle line |
| `--loudnorm` | - | Normalize audio loudness |
| `--summaries` | `summaries.json` | Import episode briefings after build |
| `--deck-name` | `show_name` | Deck name in the exported .apkg |
| `-o` | `deck.db` | Output bundle path |
| `--apkg` | `deck.apkg` | Also export .apkg immediately after build |

## Workflow

1. Get the source directory from the user
2. **Check available input sources** — look for transcript JSON files first, then external SRT/ASS, then embedded tracks (see priority order above)
3. **Identify audio tracks** — run ffprobe (see Track Selection above). Determine the Japanese audio index. Also check any external subtitle files for dual-language content before using them.
4. **Rename source video files if needed** — skip if filenames are already ASCII-safe. Only add episode numbers (`_01`, `_02`) when there are multiple videos. See `process-content.md` for full renaming rules.
5. **Generate episode summaries** — for each episode, read the subtitle text and write a translation briefing (see Episode Summary Format below). Collect all briefings into `summaries.json`.
6. **Run content2srs** — single-file or batch mode depending on input count. Use `--summaries` to attach briefings in the same build step.
7. **Export .apkg** — use `--apkg` on the build command (step 6) or run `content2srs export` separately if summaries were added after the initial build.
8. **Clean up** — delete the `deck.db` bundle and any intermediate files, leaving only the `.apkg`. **Do NOT delete transcript JSON files** — they may be needed by other workflows.
9. Report the output location to the user

## File Naming Convention

Rename source video files to this format before processing:
```
<show_name>_<episode>.mp4
```

Rules for `<show_name>`:
- All lowercase
- Use underscores for spaces
- Include season/year identifiers if relevant (e.g., `s2`, `2020`)

## Execution Steps

```bash
# 1. Store the source folder
SOURCE_DIR="/path/to/source"
CONTENT2SRS="C:/Users/snoozy/Desktop/dojo/content2srs/target/release/content2srs.exe"
SHOW_NAME="<show_name>"

# 2. Check for JSON files first, then SRT/ASS, then embedded tracks
ls "$SOURCE_DIR"/*.json 2>/dev/null
ls "$SOURCE_DIR"/*.srt "$SOURCE_DIR"/*.ass 2>/dev/null

# Always inspect audio streams — never assume stream 0 is Japanese
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "$SOURCE_DIR"/*.mkv 2>/dev/null | head -5

# If using embedded subtitle tracks, inspect those too
ffprobe -v error -select_streams s \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "$SOURCE_DIR"/*.mkv 2>/dev/null | head -5

# 3. Rename source video files to standard format
cd "$SOURCE_DIR"
for f in *.mp4; do
  num=$(echo "$f" | sed -E 's/.*[E -]([0-9]{2})[.\-].*/\1/')
  mv "$f" "${SHOW_NAME}_${num}.mp4"
done

# 4. Generate episode summaries JSON (one briefing per episode)
#    Read subtitle text and write summaries.json — format:
#    { "sources": { "ep01.mkv": "Episode 1 briefing...", "ep02.mkv": "..." } }
#    (see Episode Summary Format below)

# 5. Run content2srs — batch mode for multiple episodes
JP_AUDIO_INDEX=<index_of_jpn_audio_stream>

"$CONTENT2SRS" build -b -j 4 \
  -i "$SOURCE_DIR/"*.mkv "$SOURCE_DIR/"*.srt \
  --audio-index $JP_AUDIO_INDEX \
  --loudnorm -p 500 \
  --summaries "$SOURCE_DIR/summaries.json" \
  --deck-name "$SHOW_NAME" \
  -o "$SOURCE_DIR/${SHOW_NAME}.db" \
  --apkg "$SOURCE_DIR/${SHOW_NAME}.apkg"

# Single episode alternative:
"$CONTENT2SRS" build \
  -i "$SOURCE_DIR/${SHOW_NAME}_01.mkv" -s "$SOURCE_DIR/${SHOW_NAME}_01.srt" \
  --audio-index $JP_AUDIO_INDEX \
  --loudnorm -p 500 \
  --deck-name "$SHOW_NAME" \
  -o "$SOURCE_DIR/${SHOW_NAME}.db" \
  --apkg "$SOURCE_DIR/${SHOW_NAME}.apkg"
# Then attach briefing and re-export:
"$CONTENT2SRS" summary set -i "$SOURCE_DIR/${SHOW_NAME}.db" \
  -s "${SHOW_NAME}_01.mkv" "Episode briefing here..."
"$CONTENT2SRS" export \
  -i "$SOURCE_DIR/${SHOW_NAME}.db" \
  -o "$SOURCE_DIR/${SHOW_NAME}.apkg" \
  --deck-name "$SHOW_NAME"

# 6. Clean up bundle (apkg has everything needed for Anki)
rm "$SOURCE_DIR/${SHOW_NAME}.db" "$SOURCE_DIR/summaries.json"

# 7. Report location of output
ls -la "$SOURCE_DIR/${SHOW_NAME}.apkg"
```

## Episode Summary Format

The episode summary serves as a **translation briefing** for an LLM that will translate individual subtitle lines. It should be a mix of English and the target language, roughly 4-6 sentences, following this structure:

```
This is a line from [show name] ([name in target language]), a [format description] hosted by [host name] ([name in target language]). The guest is [guest name in target language] ([English name if applicable]), [their title/expertise/background]. They discuss [main topic in English and target language], covering [key subtopics]. Key terms that may appear: [domain-specific terms in target language with English translations]. The conversation is [register description — e.g., casual and colloquial].
```

**Example (Japanese):**
```
This is a line from ゆる言語学ラジオ (Yuru Linguistics Radio), a conversational Japanese podcast hosted by 水野太貴 (Mizuno Taiki) and 堀元見 (Horimoto Ken). They discuss linguistic misconceptions (言語学の誤解), covering topics like prescriptivism (規範主義), etymology (語源), and phonological change (音韻変化). Key terms: 言語学 (linguistics), 方言 (dialect), 音韻 (phonology). The tone is casual and humorous, with academic terminology throughout.
```

**What to include:**
- Show name and format (podcast, drama, etc.) in both English and the target language
- Host and guest names in both the target language script and romanization
- Guest's title, expertise, and relevant background
- Main topic and subtopics in both English and the target language
- Domain-specific terminology (target language term + English translation)
- Language register and conversational tone

### summaries.json format

```json
{
  "deck_summary": "Optional deck-wide fallback if an episode has no entry.",
  "sources": {
    "show_ep01.mkv": "Episode 1 briefing text...",
    "show_ep02.mkv": "Episode 2 briefing text..."
  }
}
```

Keys in `sources` must match the video filename as it appears in the bundle. Use `content2srs summary list -i deck.db` to see the exact source names registered during build.

## APKG Export

The `.apkg` is produced either by `--apkg` on the build command (all in one step) or separately:

```bash
"$CONTENT2SRS" export \
  -i "${SHOW_NAME}.db" \
  -o "${SHOW_NAME}.apkg" \
  --deck-name "$SHOW_NAME"
```

### Card Fields

content2srs produces cards with these fields: `Expression / Audio / Image / Context / Timestamps / Source`

- `Expression` — subtitle text for the card
- `Audio` — `[sound:filename.mp3]`
- `Image` — screenshot filename
- `Context` — surrounding subtitle lines, optionally prefixed with `Episode summary: <briefing> | `
- `Timestamps` — `start_ms-end_ms`
- `Source` — video filename

### APKG Notes
- The `.db` bundle contains all media blobs — the `.apkg` is derived from it. Once the `.apkg` is verified in Anki, the `.db` can be deleted.
- Deck and note type names derive from `--deck-name`; re-importing the same deck name updates existing cards rather than creating duplicates.
- `genanki` is **not** required — content2srs writes the Anki collection format natively.

## Output

The final output is a single file:
- **`<show_name>.apkg`** — complete Anki deck with all audio and screenshot files embedded, ready for direct import into Anki

## Notes

- content2srs requires text-based subtitles (SRT, ASS, or JSON). Won't work with bitmap subtitles (PGS) — use AI transcription as fallback.
- In batch mode, sidecar subtitle files are paired with videos by matching filename stems. Stems must match (e.g., `ep01.mkv` + `ep01.srt`).
- `--resume` is on by default — interrupted builds can be continued by re-running the same command.
- **Do not proactively check on background jobs.** When a long-running batch process is running, do not poll for progress unless the user asks.
