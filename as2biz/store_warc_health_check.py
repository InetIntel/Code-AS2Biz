#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Check health of *original* store WARCs, list websites (hosts) that depend on
each broken WARC (via slice index), and optionally delete the broken WARC
so that it can be re-scraped.

All archive and report paths are supplied on the command line. By default the
tool only reports broken files; deletion must be explicitly requested.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

from warcio.archiveiterator import ArchiveIterator

# Reuse the existing helpers so behaviour stays consistent
from extract_text import (
    open_warc_stream,
    canonical_no_www,
    load_slice_index,
    load_store_index,
)


def build_store_warc_to_hosts(
    archives_dir: Path,
    version_tag: str,
) -> Dict[str, Set[str]]:
    """
    From:
      - versions/<tag>/index.jsonl (slice index)
      - store/index/store_index.jsonl (store index)
    build: store_warc_name -> {host1, host2, ...}
    """
    slice_root = archives_dir / "versions" / version_tag
    slice_index = slice_root / "index.jsonl"

    store_index_path = archives_dir / "store" / "index" / "store_index.jsonl"

    if not slice_index.exists():
        raise SystemExit(f"slice index not found: {slice_index}")
    if not store_index_path.exists():
        raise SystemExit(f"store index not found: {store_index_path}")

    print(f"Loading slice index from {slice_index}")
    rows = load_slice_index(slice_index, include_screenshots=False)

    print(f"Loading store index from {store_index_path}")
    by_store_id, _ = load_store_index(store_index_path)

    store_warc_to_hosts: Dict[str, Set[str]] = defaultdict(set)

    for row in rows:
        # each HTML record normally carries a store_ref
        store_ref = row.get("store_ref") or {}
        store_id = store_ref.get("record_id")
        if not store_id:
            # some records store the payload directly in slice.warc, with no store_ref
            continue

        entry = by_store_id.get(store_id)
        if not entry:
            continue

        store_warc_name = entry.get("warc")
        if not store_warc_name:
            continue

        url = row.get("url") or ""
        host = canonical_no_www(url)
        if host:
            store_warc_to_hosts[store_warc_name].add(host)

    return store_warc_to_hosts


def check_single_warc(path: Path) -> Tuple[bool, str]:
    """
    Iterate a WARC once via open_warc_stream + warcio.
    Returns: (ok, error_message); error_message is "" when ok is True.
    """
    stream = None
    try:
        stream = open_warc_stream(path)
        it = ArchiveIterator(stream, verify_http=False)

        # iterate fully; gzip/zlib errors may only surface mid-file
        for rec in it:
            try:
                # force a payload decompression attempt
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


def find_store_warc_files(store_warc_dir: Path) -> List[Path]:
    """
    List every warc file in store/warc (.warc, .warc.gz, ...).
    """
    if not store_warc_dir.exists():
        raise SystemExit(f"Store WARC directory not found: {store_warc_dir}")

    warcs: List[Path] = []
    for p in sorted(store_warc_dir.iterdir()):
        if not p.is_file():
            continue
        # simple check: ".warc" appears in the suffixes
        if ".warc" in "".join(p.suffixes):
            warcs.append(p)

    return warcs


def main():
    ap = argparse.ArgumentParser(
        description="Check health of original store WARCs, list affected websites from slice index, and optionally delete broken WARCs."
    )
    ap.add_argument(
        "--archives-dir",
        required=True,
        help="Root archives directory (same one used by extract_text.py).",
    )
    ap.add_argument(
        "--version-tag",
        required=True,
        help="Version tag under archives/versions/<tag>, used to map store records → URLs.",
    )
    ap.add_argument(
        "--delete-broken",
        type=lambda x: str(x).lower() == "true",
        default=False,
        help="Whether to actually delete broken store WARCs (true/false). Default: false.",
    )
    ap.add_argument(
        "--report-json",
        default=None,
        help="Optional path to write a JSON summary of broken store WARCs and their websites.",
    )

    args = ap.parse_args()

    archives_dir = Path(args.archives_dir).expanduser().resolve()
    store_warc_dir = archives_dir / "store" / "warc"

    print(f"Archives dir:    {archives_dir}")
    print(f"Store WARC dir:  {store_warc_dir}")
    print(f"Version tag:     {args.version_tag}")
    print()

    # 1) build the store_warc -> hosts map from the indexes
    print("Building store_warc -> hosts mapping from indexes...")
    store_warc_to_hosts = build_store_warc_to_hosts(
        archives_dir=archives_dir,
        version_tag=args.version_tag,
    )
    print(f"Index gives host info for {len(store_warc_to_hosts)} store WARC file(s).")
    print()

    # 2) list every store/warc file
    store_warc_files = find_store_warc_files(store_warc_dir)
    print(f"Found {len(store_warc_files)} WARC file(s) in {store_warc_dir}.")
    print()

    broken_summary = []

    # 3) health-check each store.warc
    for path in store_warc_files:
        warc_name = path.name
        print(f"Checking {warc_name} ...", flush=True)

        ok, err = check_single_warc(path)
        if ok:
            print("  ✅ OK")
            continue

        # broken store WARC
        print(f"  ❌ BROKEN: {err}")

        hosts = sorted(store_warc_to_hosts.get(warc_name, []))
        if hosts:
            print("  Websites (canonical hosts) using this store WARC (from slice index):")
            for h in hosts:
                print(f"    - {h}")
        else:
            print("  (No hosts found for this store WARC in slice index; "
                  "it may only contain unused or non-HTML records.)")

        broken_summary.append(
            {
                "warc": warc_name,
                "path": str(path),
                "error": err,
                "websites": hosts,
            }
        )

        # 4) delete the broken WARC if requested
        if args.delete_broken:
            try:
                path.unlink()
                print(f"  🗑️ Deleted broken store WARC: {path}")
            except Exception as e:
                print(f"  ⚠️ Failed to delete {path}: {e}", file=sys.stderr)

        print()

    # 5) write the JSON report
    if args.report_json:
        report_path = Path(args.report_json).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8") as f:
            json.dump({"broken_store_warcs": broken_summary}, f, indent=2)
        print(f"Summary written to {report_path}")

    # final summary
    if broken_summary:
        print()
        print(f"Done. Found {len(broken_summary)} broken store WARC file(s).")
    else:
        print()
        print("Done. No broken store WARCs found.")


if __name__ == "__main__":
    main()
