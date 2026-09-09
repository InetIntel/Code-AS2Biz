"""
One-time backfill: build a NEW, separate index file that records each store
WARC record's byte offset + compressed length within its store_*.warc.gz file,
so future extraction can seek() directly instead of linearly scanning every
record in the file.

READ-ONLY with respect to everything that already exists:
  - does not open store_*.warc.gz for writing
  - does not touch store_index.jsonl
  - writes only to a brand-new output file (default: store_index_offsets.jsonl)

Usage:
    python3 build_store_offset_index.py \
        --archives-dir archives \
        --out archives/store/index/store_index_offsets.jsonl \
        --workers 48
"""
import argparse
import json
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

from warcio.archiveiterator import ArchiveIterator


def scan_one_file(warc_path_str: str):
    warc_path = Path(warc_path_str)
    rows = []
    n_err = 0
    if warc_path.stat().st_size == 0:
        return warc_path.name, rows, 0
    try:
        with open(warc_path, "rb") as f:
            it = ArchiveIterator(f)
            for record in it:
                try:
                    rec_id = record.rec_headers.get_header("WARC-Record-ID")
                    if not rec_id:
                        n_err += 1
                        continue
                    offset = it.get_record_offset()
                    length = it.get_record_length()
                    rows.append({
                        "warc": warc_path.name,
                        "record_id": rec_id,
                        "offset": offset,
                        "gzlen": length,
                    })
                except Exception:
                    n_err += 1
    except Exception as e:
        print(f"⚠️  Failed to scan {warc_path.name}: {e}")
    return warc_path.name, rows, n_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=48)
    args = ap.parse_args()

    store_dir = Path(args.archives_dir) / "store" / "warc"
    out_path = Path(args.out)
    if out_path.exists():
        raise SystemExit(f"❌ Refusing to overwrite existing file: {out_path}")

    files = sorted(store_dir.glob("*.warc.gz"))
    print(f"Found {len(files)} store WARC files under {store_dir}")

    total_rows = 0
    total_err = 0
    t0 = time.time()
    tmp_out = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp_out, "w", encoding="utf-8") as out_f, \
         ProcessPoolExecutor(max_workers=args.workers) as executor:
        future_to_name = {
            executor.submit(scan_one_file, str(p)): p.name for p in files
        }
        done = 0
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            done += 1
            try:
                fname, rows, n_err = future.result()
            except Exception as e:
                print(f"❌ {name}: worker crashed: {e}")
                continue
            for row in rows:
                out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            total_rows += len(rows)
            total_err += n_err
            if done % 10 == 0 or done == len(files):
                elapsed = time.time() - t0
                print(f"[{done}/{len(files)}] {fname}: +{len(rows)} rows "
                      f"(err={n_err}) | total_rows={total_rows} elapsed={elapsed:.1f}s")

    tmp_out.rename(out_path)
    print(f"\n✅ Done. {total_rows} rows written to {out_path} "
          f"({total_err} record parse errors) in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
