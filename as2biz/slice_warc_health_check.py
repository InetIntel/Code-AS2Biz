#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Check health of slice WARC files, list websites inside broken WARCs from index,
and optionally delete those WARCs so they can be re-scraped.

Usage:

python warc_health_check.py \
  --archives-dir ./archives \
  --version-tag 2025-01-01 \
  --delete-broken true \
  --report-json ./tmp/bad_warcs_2025-01-01.json

What it does:
1. Looks at: archives/versions/<version_tag>/warc
2. Uses:     archives/versions/<version_tag>/index.jsonl
3. For each *.warc / *.warc.gz:
   - tries to read it fully via warcio + open_warc_stream()
   - if any exception occurs, marks it as broken
4. For broken WARCs:
   - finds all URLs in index.jsonl whose "warc" field equals this WARC name
   - converts URLs to hostnames via canonical_no_www()
   - prints and records the unique website hosts
   - optionally deletes the WARC file if --delete-broken true
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

from warcio.archiveiterator import ArchiveIterator

# Reuse helpers from your existing code
from extract_text import open_warc_stream, canonical_no_www


def load_warc_hosts_from_index(index_path: Path) -> Dict[str, Set[str]]:
    """
    Build mapping: warc_filename -> set of canonical hosts
    using the slice index JSONL.

    Expects lines like:
        {"warc": "slice_00001.warc.gz", "url": "https://example.com/...", ...}
    """
    warc_to_hosts: Dict[str, Set[str]] = defaultdict(set)

    if not index_path.exists():
        raise SystemExit(f"Index file not found: {index_path}")

    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            warc_name = obj.get("warc")
            if not warc_name:
                continue

            # URL field: in your slice index this is "url"
            url = obj.get("url") or ""
            if not url:
                continue

            host = canonical_no_www(url)
            if host:
                warc_to_hosts[warc_name].add(host)

    return warc_to_hosts


def check_single_warc(path: Path) -> Tuple[bool, str]:
    """
    Try to read a WARC file fully via warcio + open_warc_stream.
    Returns (ok, error_message). If ok is True, error_message is "".
    """
    stream = None
    try:
        stream = open_warc_stream(path)
        it = ArchiveIterator(stream, verify_http=False)

        # We iterate through the entire file; gzip/zlib errors can appear late.
        for rec in it:
            try:
                # Ensure payload decompression is attempted
                _ = rec.content_stream().read(1)
            except Exception as e:
                return False, f"Error reading record payload: {e}"

        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def find_warc_files(warc_dir: Path) -> List[Path]:
    """
    Find WARC files under warc_dir. Handles .warc, .warc.gz, and similar.
    """
    if not warc_dir.exists():
        raise SystemExit(f"WARC directory not found: {warc_dir}")

    warcs: List[Path] = []
    for p in sorted(warc_dir.iterdir()):
        if not p.is_file():
            continue
        # Heuristic: file name contains ".warc"
        if ".warc" in "".join(p.suffixes):
            warcs.append(p)

    return warcs


def main():
    ap = argparse.ArgumentParser(
        description="Check health of slice WARCs, list affected websites from index, and optionally delete broken WARCs."
    )
    ap.add_argument(
        "--archives-dir",
        required=True,
        help="Root archives directory (same one used by extract_text.py).",
    )
    ap.add_argument(
        "--version-tag",
        required=True,
        help="Version tag under archives/versions/<tag>.",
    )
    ap.add_argument(
        "--delete-broken",
        type=lambda x: str(x).lower() == "true",
        default=False,
        help="Whether to actually delete broken WARCs (true/false). Default: false.",
    )
    ap.add_argument(
        "--report-json",
        default=None,
        help="Optional path to write a JSON summary of broken WARCs and their websites.",
    )

    args = ap.parse_args()

    archives_dir = Path(args.archives_dir).expanduser().resolve()
    slice_root = archives_dir / "versions" / args.version_tag
    warc_dir = slice_root / "warc"
    index_path = slice_root / "index.jsonl"

    print(f"Archives dir: {archives_dir}")
    print(f"Slice root:   {slice_root}")
    print(f"WARC dir:     {warc_dir}")
    print(f"Index file:   {index_path}")
    print()

    # 1) Load warc -> hosts mapping from index
    print("Loading warc -> host mapping from index...")
    warc_to_hosts = load_warc_hosts_from_index(index_path)
    print(f"Index provides host info for {len(warc_to_hosts)} WARC file(s).")
    print()

    # 2) Enumerate WARC files
    warc_files = find_warc_files(warc_dir)
    print(f"Found {len(warc_files)} WARC file(s) in {warc_dir}.")
    print()

    broken_summary = []

    # 3) Health check each WARC
    for path in warc_files:
        warc_name = path.name
        print(f"Checking {warc_name} ...", flush=True)

        ok, err = check_single_warc(path)
        if ok:
            print("  ✅ OK")
            continue

        # Broken WARC
        print(f"  ❌ BROKEN: {err}")

        hosts = sorted(warc_to_hosts.get(warc_name, []))
        if hosts:
            print("  Websites (canonical hosts) in this WARC (from index):")
            for h in hosts:
                print(f"    - {h}")
        else:
            print("  (No hosts found for this WARC in index.jsonl)")

        # Collect summary
        broken_summary.append(
            {
                "warc": warc_name,
                "path": str(path),
                "error": err,
                "websites": hosts,
            }
        )

        # Optionally delete file
        if args.delete_broken:
            try:
                path.unlink()
                print(f"  🗑️ Deleted broken WARC: {path}")
            except Exception as e:
                print(f"  ⚠️ Failed to delete {path}: {e}", file=sys.stderr)

        print()

    # 4) Optional JSON report
    if args.report_json:
        report_path = Path(args.report_json).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8") as f:
            json.dump({"broken_warcs": broken_summary}, f, indent=2)
        print(f"Summary written to {report_path}")

    # Final summary
    if broken_summary:
        print()
        print(f"Done. Found {len(broken_summary)} broken WARC file(s).")
    else:
        print()
        print("Done. No broken WARCs found.")


if __name__ == "__main__":
    main()
