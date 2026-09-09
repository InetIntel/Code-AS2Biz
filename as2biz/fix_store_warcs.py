#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import gzip
import argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

from warcio.archiveiterator import ArchiveIterator
from warcio.warcwriter import WARCWriter
from tqdm import tqdm


def open_warc_stream_compatible(path: Path):
    """
    Compatible read: whether the file is a single gzip stream or standard
    member-gzip, return a binary stream that ArchiveIterator can consume.
    """
    with open(path, "rb") as f:
        head = f.read(2)

    if head == b"\x1f\x8b":
        # gzip.open returns a file-like object that supports "with"
        return gzip.open(path, "rb")
    else:
        return open(path, "rb")


def fix_single_warc(file_path: Path):
    """
    Worker process: repair one WARC file.
    Returns: (success: bool, record_count: int, message: str)
    """
    temp_path = file_path.with_name(file_path.name + ".tmp")

    try:
        count = 0

        # 1. read the source (compat mode), write a new standard member-gzip file
        with open_warc_stream_compatible(file_path) as src_stream, \
             open(temp_path, "wb") as dest_f:

            writer = WARCWriter(dest_f, gzip=True)

            for record in ArchiveIterator(src_stream):
                writer.write_record(record)
                count += 1

        # 2. treat it as success only if at least one record was written
        if count == 0:
            if temp_path.exists():
                temp_path.unlink()
            return False, 0, f"⚠️  No records found in {file_path.name}"

        # 3. atomically replace the original
        os.replace(temp_path, file_path)
        return True, count, ""

    except Exception as e:
        # clean up the temp file
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        return False, 0, f"❌ Failed to fix {file_path.name}: {e}"


def main():
    parser = argparse.ArgumentParser(
        description="Parallel convert monolithic GZIP WARCs to standard member-GZIP."
    )
    parser.add_argument(
        "--archives_dir",
        required=True,
        help="Root archives dir (containing store/warc)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(8, os.cpu_count() or 4)),
        help="Number of parallel processes (default: min(8, cpu_count))",
    )
    args = parser.parse_args()

    store_warc_dir = Path(args.archives_dir) / "store" / "warc"
    if not store_warc_dir.exists():
        sys.exit(f"Store dir not found: {store_warc_dir}")

    warc_files = sorted(
        p for p in store_warc_dir.iterdir()
        if p.is_file() and p.name.endswith(".warc.gz")
    )

    total_files = len(warc_files)
    if total_files == 0:
        sys.exit("No .warc.gz files found, nothing to do.")

    print(f"Found {total_files} Store WARCs.")
    print(f"Starting repair with {args.workers} workers...")

    success_files = 0
    total_records = 0
    errors = []

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        future_to_file = {
            executor.submit(fix_single_warc, p): p
            for p in warc_files
        }

        pbar = tqdm(as_completed(future_to_file), total=total_files, unit="file")

        for future in pbar:
            fpath = future_to_file[future]
            try:
                ok, recs, msg = future.result()
                if ok:
                    success_files += 1
                    total_records += recs
                    # optionally print the record count of a fixed file
                    # pbar.write(f"✅ {fpath.name}: {recs} records")
                else:
                    errors.append(msg)
                    pbar.write(msg)
            except Exception as exc:
                msg = f"❌ Unhandled exception for {fpath.name}: {exc}"
                errors.append(msg)
                pbar.write(msg)

    print("\n" + "=" * 40)
    print("✅ Repair Complete.")
    print(f"   Fixed Files: {success_files}/{total_files}")
    print(f"   Total Records Processed: {total_records}")

    if errors:
        print(f"\n⚠️  Encountered {len(errors)} error(s):")
        for e in errors[:10]:
            print(f"   {e}")
        if len(errors) > 10:
            print("   ... and more.")
    print("=" * 40)
    print("You can now safely run your prepare_openai_batch pipeline on these WARCs.")

if __name__ == "__main__":
    main()