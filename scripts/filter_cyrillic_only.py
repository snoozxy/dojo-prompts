#!/usr/bin/env python3
"""Drop SRT cues that are entirely Russian/Cyrillic (no Japanese kana/kanji).

Keeps any cue containing Japanese kana or kanji, even if it also has
Cyrillic text mixed in (e.g. a blushing Russian aside embedded in a
Japanese narration line). Only drops cues with Cyrillic content and
zero Japanese kana/kanji, since those aren't useful for a Japanese
learning deck.
"""
import re
import sys

JAPANESE_RE = re.compile(r'[぀-ゟ゠-ヿ一-鿿]')
CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')

def filter_srt(path):
    with open(path, encoding='utf-8') as f:
        content = f.read()
    blocks = content.strip().split('\n\n')
    kept = []
    dropped = 0
    for b in blocks:
        lines = b.split('\n')
        if len(lines) < 3:
            continue
        text = '\n'.join(lines[2:])
        if CYRILLIC_RE.search(text) and not JAPANESE_RE.search(text):
            dropped += 1
            continue
        kept.append(lines[1:])  # keep timing + text, renumber index below

    out_lines = []
    for i, block_lines in enumerate(kept, 1):
        out_lines.append(str(i))
        out_lines.extend(block_lines)
        out_lines.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out_lines).rstrip('\n') + '\n')
    return dropped, len(kept)

if __name__ == '__main__':
    for path in sys.argv[1:]:
        dropped, kept = filter_srt(path)
        print(f"{path}: dropped {dropped} Cyrillic-only cues, {kept} remain")
