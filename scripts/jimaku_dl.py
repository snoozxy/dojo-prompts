#!/usr/bin/env python3
"""Download Japanese subtitles from jimaku.cc.

Environment:
    JIMAKU_API_KEY   your jimaku.cc API key (from jimaku.cc/account)

Commands:
    search <query>                           search entries by title
    files  <entry_id> [--episode N]          list subtitle files for an entry
    download <entry_id> --out <dir>          download all subtitle files
    download <entry_id> --episode N --out <dir>  download files for one episode

Authentication header: Authorization: <key>  (no "Bearer" prefix)
Rate limit: 25 requests/minute per IP.
"""
import argparse
import io
import os
import sys
import zipfile

import requests

JIMAKU_API = "https://jimaku.cc/api"
SUB_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt")


def _key() -> str:
    k = os.environ.get("JIMAKU_API_KEY")
    if not k:
        sys.exit("JIMAKU_API_KEY is not set. Generate one at jimaku.cc/account.")
    return k


def _auth() -> dict:
    return {"Authorization": _key()}


def _ok(r: requests.Response, label: str):
    if not r.ok:
        sys.exit(f"jimaku {label} error {r.status_code}: {r.text[:400]}")


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_search(args):
    params = {"query": args.query}
    if not args.all:
        params["anime"] = "true"
    r = requests.get(f"{JIMAKU_API}/entries/search", headers=_auth(),
                     params=params, timeout=30)
    _ok(r, "search")
    entries = r.json()
    if not entries:
        print("No entries found.")
        return
    for e in entries:
        ef = e.get("flags", {})
        tags = [t for t in ("anime", "movie", "unverified") if ef.get(t)]
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  id={e['id']}  {e['name']}{tag_str}")
        if e.get("english_name"):  print(f"           en: {e['english_name']}")
        if e.get("japanese_name"): print(f"           jp: {e['japanese_name']}")


def cmd_files(args):
    params = {}
    if args.episode is not None:
        params["episode"] = args.episode
    r = requests.get(f"{JIMAKU_API}/entries/{args.entry_id}/files",
                     headers=_auth(), params=params, timeout=30)
    _ok(r, "files")
    files = r.json()
    if not files:
        print("No files found.")
        return
    for f in files:
        kb = f["size"] // 1024
        print(f"  {f['name']}  ({kb} KB)  modified={f['last_modified']}")
        print(f"    {f['url']}")


def cmd_download(args):
    out_dir = args.out or "."
    os.makedirs(out_dir, exist_ok=True)

    params = {}
    if args.episode is not None:
        params["episode"] = args.episode
    r = requests.get(f"{JIMAKU_API}/entries/{args.entry_id}/files",
                     headers=_auth(), params=params, timeout=30)
    _ok(r, "files")
    files = r.json()
    if not files:
        print("No files found for this entry/episode.")
        return

    downloaded = []
    for f in files:
        name = f["name"]
        print(f"Downloading {name}...")
        resp = requests.get(f["url"], timeout=120)
        resp.raise_for_status()

        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                for member in zf.namelist():
                    if member.lower().endswith(SUB_EXTENSIONS):
                        out_name = os.path.basename(member)
                        if not out_name:
                            continue
                        out_path = os.path.join(out_dir, out_name)
                        with zf.open(member) as src, open(out_path, "wb") as dst:
                            dst.write(src.read())
                        print(f"  Extracted: {out_name}")
                        downloaded.append(out_path)
        elif name.lower().endswith(SUB_EXTENSIONS):
            out_path = os.path.join(out_dir, name)
            with open(out_path, "wb") as fh:
                fh.write(resp.content)
            print(f"  Saved: {name}")
            downloaded.append(out_path)
        else:
            print(f"  Skipped (not a subtitle or zip): {name}")

    print(f"\n{len(downloaded)} subtitle file(s) saved to {out_dir}:")
    for p in downloaded:
        print(f"  {p}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="jimaku.cc subtitle downloader")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="Search for entries by title")
    p.add_argument("query", help="Show title (romaji, English, or Japanese)")
    p.add_argument("--all", action="store_true",
                   help="Include non-anime entries (live action, dramas, etc.)")

    p = sub.add_parser("files", help="List subtitle files for an entry")
    p.add_argument("entry_id", type=int)
    p.add_argument("--episode", type=int, default=None,
                   help="Filter to files matching this episode number")

    p = sub.add_parser("download", help="Download subtitle files")
    p.add_argument("entry_id", type=int)
    p.add_argument("--episode", type=int, default=None,
                   help="Download only files matching this episode number")
    p.add_argument("--out", default=None,
                   help="Output directory (default: current directory)")

    args = ap.parse_args()
    {"search": cmd_search, "files": cmd_files, "download": cmd_download}[args.cmd](args)


if __name__ == "__main__":
    main()
