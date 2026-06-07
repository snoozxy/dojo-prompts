#!/usr/bin/env python3
"""Combine multiple subs2cia TSV files into a single file.

Reads the header from the first file, then appends all data rows from all
files in sorted order. Works correctly on Windows (unlike head -q -1).

Usage:
    python3 combine_tsv.py <tsv_dir> <output.tsv>
    python3 combine_tsv.py <tsv_dir> <output.tsv> --glob "*.tsv"

Arguments:
    tsv_dir     - Directory containing TSV files to combine
    output.tsv  - Output path (can be inside tsv_dir)
"""

import argparse
import csv
import glob
import os
import sys


def main():
    ap = argparse.ArgumentParser(description="Combine subs2cia TSV files into one.")
    ap.add_argument("tsv_dir", help="Directory containing TSV files")
    ap.add_argument("output", help="Output TSV path")
    ap.add_argument("--glob", default="*.tsv",
                    help="Filename glob to match input files (default: *.tsv)")
    args = ap.parse_args()

    output_abs = os.path.abspath(args.output)
    files = sorted(
        f for f in glob.glob(os.path.join(args.tsv_dir, args.glob))
        if os.path.abspath(f) != output_abs
    )
    if not files:
        sys.exit(f"No TSV files found in {args.tsv_dir} matching {args.glob!r}")

    header = None
    total_rows = 0

    with open(args.output, "w", encoding="utf-8", newline="") as out_f:
        writer = None
        for path in files:
            with open(path, encoding="utf-8", newline="") as in_f:
                reader = csv.reader(in_f, delimiter="\t")
                file_header = next(reader, None)
                if file_header is None:
                    continue
                if header is None:
                    header = file_header
                    writer = csv.writer(out_f, delimiter="\t")
                    writer.writerow(header)
                for row in reader:
                    writer.writerow(row)
                    total_rows += 1

    print(f"Combined {len(files)} TSV(s), {total_rows} rows → {args.output}")


if __name__ == "__main__":
    main()
