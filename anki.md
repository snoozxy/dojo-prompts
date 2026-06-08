---
name: anki
description: |
  Create subs2srs Anki decks from video files using subs2cia. Generates audio
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

Create subs2srs Anki decks from video files using subs2cia. Generates audio clips and subtitle text for flashcard-based language learning.

## CRITICAL: Required Fork of subs2cia

This skill **requires** [snoozxy's fork of subs2cia](https://github.com/snoozxy/subs2cia). The original/upstream subs2cia will NOT work — it lacks the `context` column, `--export-header-row`, and BCP 47 locale tag support that this skill depends on.

**If a different version of subs2cia is already installed, you MUST uninstall it first:**
```bash
pip3 uninstall subs2cia
pip3 install git+https://github.com/snoozxy/subs2cia.git
```

**If the correct fork is already installed, upgrade it to ensure you have the latest features:**
```bash
pip3 install --upgrade git+https://github.com/snoozxy/subs2cia.git
```

Do not proceed until the correct fork is installed and up to date.

### Performance (snoozxy fork)

- **`srs` reads audio directly from the source container** via `-ai` — no upfront FLAC demux.
- **Batch multi-episode runs**: use `-b -j N` (e.g. `-j 4`). `hw_probe.py` caches `SUBS2CIA_JOBS` and `SUBS2CIA_WORKERS`.
- **Single episode**: `-j` is optional; per-card parallelism still uses `SUBS2CIA_WORKERS` from the cache.
- Audio clips use fast input-side seek (few ms tolerance; `-p` padding covers it).

## Usage

The user provides a source directory containing video files (typically .mp4 with target language audio and subtitles). The skill processes all videos and outputs a single .apkg Anki deck. The user is an English speaker. The default target language is **Japanese**, but the user may specify another language.

## Track Selection

**Always run ffprobe before any subs2cia command.** Never assume stream 0 is Japanese — many encodes (dual-audio WEBRips, EMBER, etc.) put English audio first. The wrong stream produces garbage cards.

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

Find the stream index where `language` is `jpn`, `ja`, `japanese`, or `日本語`. Use that index for `-ai` (audio) and `-si` (subtitles). If there are multiple Japanese tracks, ask the user which to use.

### Other Languages
Adapt track selection to whatever language the user specifies. Use the same ffprobe approach — match by language code and title tags.

### Multi-language subtitle files

Dual-language fansub ASS files and some jimaku downloads contain both Japanese and English lines in a single file. Passing these directly to subs2cia will mix both languages into card text. Before using an external subtitle file, check whether it's pure Japanese:

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

If `latin-only lines` is significant (more than a handful of timing/metadata lines), the file is likely dual-language. Either:
- Find a Japanese-only subtitle file from the jimaku download set, or
- Strip English lines from the ASS (for ASS files, English lines typically use a style named `Default` or `English` while Japanese uses `Japanese` or `Subtitle`).

### Check Available Input Sources

Before running subs2cia, check for input sources in priority order:

**1. transcript JSON files (preferred)** — Check if there are transcript JSON files alongside the videos:
```bash
ls "$SOURCE_DIR"/*.json 2>/dev/null
```
If JSON files exist, use them as input. subs2cia will use MeCab-based sentence segmentation to produce one card per sentence — this gives better card boundaries than SRT-based splitting. JSON files must be passed explicitly alongside the video in `-i`.

**2. External SRT/ASS files (fallback)** — Check if there are subtitle files alongside the videos:
```bash
ls "$SOURCE_DIR"/*.srt "$SOURCE_DIR"/*.ass 2>/dev/null
```
If external subtitle files exist (e.g., from the create-srt skill), subs2cia will pick them up automatically as long as the filename matches the video (e.g., `video.mp4` + `video.srt`). In this case you don't need `-si` at all.

**3. Embedded tracks (last resort)** — Inspect the video files for embedded audio and subtitle tracks:
```bash
ffprobe -v error -select_streams a -show_entries stream=index:stream_tags=language,title -of csv=p=0 "$SOURCE_DIR"/*.mp4 2>/dev/null | head -5
ffprobe -v error -select_streams s -show_entries stream=index:stream_tags=language,title -of csv=p=0 "$SOURCE_DIR"/*.mp4 2>/dev/null | head -5
```

## Base Command

**Requires [snoozxy's fork](https://github.com/snoozxy/subs2cia) — see "Required Fork" section above.**

```bash
# With JSON (preferred — MeCab sentence segmentation)
# Audio index still required — check with ffprobe first (see Track Selection above)
subs2cia srs -i "video.mp4" "transcript.json" -ai <jp_audio_index> -p 500 -N -d out_srs --export-header-row

# With external SRT (fallback — subs2cia picks it up by filename match)
# Still need -ai for correct audio; -si is not needed for external files
# -j: parallel episodes (from hw_probe cache or SUBS2CIA_JOBS); omit for single file
subs2cia srs -b -j 4 -i "*.mp4" -ai <jp_audio_index> -p 500 -N -d out_srs --export-header-row

# With embedded subtitle tracks (last resort)
# Both -ai and -si must come from ffprobe — never hardcode 0
subs2cia srs -b -j 4 -i "*.mp4" -ai <jp_audio_index> -si <jp_subtitle_index> -p 500 -N -d out_srs --export-header-row
```

### Parameters Explained

| Flag | Value | Purpose |
|------|-------|---------|
| `srs` | - | SRS subcommand (creates Anki-ready output) |
| `-b` | - | Batch mode (process multiple files) |
| `-j` | `4` | Parallel episodes in batch mode (from `hw_probe` cache or `SUBS2CIA_JOBS`) |
| `-i` | `"*.mp4" "*.json"` | Input files (video + JSON or video + subtitle) |
| `-ai` | `0` | Japanese audio stream index in the source container (always required) |
| `-si` | `0` | Subtitle stream index (only needed with embedded tracks) |
| `-p` | `500` | Padding in ms around each subtitle line |
| `-N` | - | Normalize audio levels |
| `-d` | `out_srs` | Output directory name |
| `--export-header-row` | - | Include column headers in TSV output |

## Workflow

1. Get the source directory from the user
2. **Check available input sources** — look for transcript JSON files first, then external SRT/ASS, then embedded tracks (see priority order above)
3. **Identify audio and subtitle tracks** — run ffprobe on both audio and subtitle streams (see Track Selection above). Determine the Japanese audio index (`-ai`) and, if using embedded subs, the Japanese subtitle index (`-si`). Also check any external subtitle files for dual-language content before using them.
4. **Rename source video files if needed** — skip if filenames are already ASCII-safe. Only add episode numbers (`_01`, `_02`) when there are multiple videos. See `process-content.md` for full renaming rules.
5. Navigate to the source directory
6. Run subs2cia with JSON input (preferred) or subtitle track indices (fallback)
7. **Generate episode summaries** - for each TSV, read subtitle text and generate a translation briefing (see Episode Summary Format below), then prepend it to every row's `context` column. Use subagents to process all TSVs in parallel.
8. **Combine all TSV files** into a single `combined.tsv`
9. **Export as .apkg** - package the combined TSV and all media files into an Anki .apkg deck, saved to the source directory
10. **Clean up** - delete the `out_srs/` directory and all intermediate files, leaving only the .apkg. If an `.anki.srt` was generated from a transcript JSON file, delete it too — the SRT is an intermediate artifact, not a final output. **Do NOT delete the transcript JSON file** — it may be needed by other workflows.
11. Report the output location to the user

## File Naming Convention

Rename source video files to this format before processing:
```
<show_name>_<episode>.mp4
```

This ensures output files automatically follow the naming convention:
- `<show_name>_<episode>_<start>-<end>.mp3`

Examples:
- `kiseijuu_01.mp4` → `kiseijuu_01_585-3377.mp3`
- `oshi_no_ko_s2_13.mp4` → `oshi_no_ko_s2_13_4797-8508.mp3`

Rules for `<show_name>`:
- All lowercase
- Use underscores for spaces
- Include season/year identifiers if relevant (e.g., `s2`, `2020`)

## Execution Steps

```bash
# 1. Store the source folder
SOURCE_DIR="/path/to/source"

# 2. Check for JSON files first, then SRT/ASS, then embedded tracks
ls "$SOURCE_DIR"/*.json 2>/dev/null
ls "$SOURCE_DIR"/*.srt "$SOURCE_DIR"/*.ass 2>/dev/null

# Always inspect audio streams — never assume stream 0 is Japanese
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "$SOURCE_DIR"/*.mp4 2>/dev/null

# If using embedded subtitle tracks, inspect those too
ffprobe -v error -select_streams s \
  -show_entries stream=index:stream_tags=language,title \
  -of csv=p=0 "$SOURCE_DIR"/*.mp4 2>/dev/null

# Set these based on ffprobe output before running subs2cia
JP_AUDIO_INDEX=<index_of_jpn_audio_stream>
JP_SUB_INDEX=<index_of_jpn_subtitle_stream>   # only if using embedded subs

# 3. Rename source video files to standard format
# Ask user for the show name (e.g., "kiseijuu", "oshi_no_ko_s2")
SHOW_NAME="<show_name>"
cd "$SOURCE_DIR"
for f in *.mp4; do
  # Extract episode number (handles formats like "E01", "- 01", " 01.")
  num=$(echo "$f" | sed -E 's/.*[E -]([0-9]{2})[.\-].*/\1/')
  mv "$f" "${SHOW_NAME}_${num}.mp4"
done

# 4. Run subs2cia — prefer JSON, fall back to SRT
# With JSON (preferred) — -ai is still required:
PYTHONUTF8=1 subs2cia srs -i "video.mp4" "transcript.json" -ai $JP_AUDIO_INDEX -p 500 -N -d out_srs --export-header-row
# With external SRT (fallback) — -si not needed, external file is picked up by name:
PYTHONUTF8=1 subs2cia srs -b -j 4 -i "*.mp4" -ai $JP_AUDIO_INDEX -p 500 -N -d out_srs --export-header-row
# With embedded subs (last resort) — both indices required:
PYTHONUTF8=1 subs2cia srs -b -j 4 -i "*.mp4" -ai $JP_AUDIO_INDEX -si $JP_SUB_INDEX -p 500 -N -d out_srs --export-header-row

# 5. Generate episode summaries and prepend to context column
#    Launch subagents in parallel (one per TSV) to:
#    a) Read subtitle text from the 'text' column
#    b) Generate a translation briefing (see Episode Summary Format below)
#    c) Prepend "Episode summary: <briefing> | " to every row's context column
#    Use this Python snippet to apply the summary to a single TSV:

# EPISODE_SUMMARY should be set per-file after reading and summarizing the text
python3 dojo-prompts/scripts/prepend_summary.py out_srs/<filename>.tsv "EPISODE_SUMMARY_HERE"

# 6. Combine all TSV files into a single file
python3 dojo-prompts/scripts/combine_tsv.py out_srs/ out_srs/combined.tsv

# 7. Export as .apkg
#    Output goes to $SOURCE_DIR/<show_name>.apkg
python3 dojo-prompts/scripts/apkg_export.py out_srs/combined.tsv out_srs/ "${SHOW_NAME}" "$SOURCE_DIR"

# 8. Clean up intermediate files
rm -rf out_srs/

# 9. Report location of output
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

**Example (Chinese):**
```
This is a line from 博音 (Bo Yin Podcast), a conversational Mandarin Chinese podcast hosted by 博恩 (Brian Tseng). The guest is 何立安博士, a sports science PhD specializing in strength training and physical conditioning. They discuss why people plateau in weight training (重訓卡關), covering topics like training discipline (紀律), progressive overload (漸進式超負荷), and the science behind muscle adaptation. Key terms: 重訓 (weight training), 卡關 (hitting a plateau), 肌肥大 (muscle hypertrophy). The tone is casual and colloquial, with technical fitness terminology throughout.
```

**What to include:**
- Show name and format (podcast, drama, etc.) in both English and the target language
- Host and guest names in both the target language script and romanization
- Guest's title, expertise, and relevant background
- Main topic and subtopics in both English and the target language
- Domain-specific terminology (target language term + English translation)
- Language register and conversational tone

## APKG Export

After combining TSVs, package everything into an Anki .apkg file using the `apkg_export.py` script:

```bash
python3 dojo-prompts/scripts/apkg_export.py out_srs/combined.tsv out_srs/ "${SHOW_NAME}" "$SOURCE_DIR"
```

### APKG Notes
- The model uses a **listening card** template: front = audio only, back = text + context
- Deck and model IDs are derived from the show name so re-importing updates existing cards rather than creating duplicates
- The `genanki` package must be installed (`pip3 install genanki`)
- Screenshots are exported by default by subs2cia (disable with `--no-export-screenshot`)
- The TSV column names (`audioclip`, `screenclip`, `text`, `context`) come from subs2cia's `--export-header-row` output. The `audioclip` column contains `[sound:filename.mp3]` format and `screenclip` contains `<img src='filename.jpg'>` format — both need parsing to extract the bare filename.

## Output

The final output is a single file in the source directory:
- **`<show_name>.apkg`** - complete Anki deck with all audio and screenshot files embedded, ready for direct import into Anki

All intermediate files (TSVs, audio clips, screenshots, the `out_srs/` directory) are deleted after the .apkg is created.

## Adjustments

- **Different video format**: Change `*.mp4` to `*.mp4` or other format
- **Different track indices**: Adjust `-ai` and `-si` based on ffprobe output
- **More/less padding**: Adjust `-p` value (default 500ms)

## Notes

- subs2cia requires text-based subtitles (SRT, ASS). Won't work with bitmap subtitles (PGS).
- subs2cia picks up external subtitle files automatically if the filename matches the video (e.g., `video.mp4` + `video.srt`).
- If subtitles are embedded, subs2cia extracts them automatically.
- The .apkg is written directly to the source directory; all intermediate files are cleaned up automatically.
- **Do not proactively check on background jobs.** When a long-running batch process is running in the background, do not poll for progress or read output files unless the user asks. This avoids wasting context window on progress bar output.
