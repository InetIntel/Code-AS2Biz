# file: rebuild_slice_tool.py
import json
import os
from datetime import datetime, timezone
from warcio.warcwriter import WARCWriter

def rebuild_slice_warc(
    archives_dir: str,
    version_tag: str,
    bad_slice_filename: str,
    broken_store_files: list
):
    """
    Read index.jsonl and rebuild a new slice WARC.
    Written as member-gzip for better corruption resistance.
    """
    version_dir = os.path.join(archives_dir, "versions", version_tag)
    original_index_path = os.path.join(version_dir, "index.jsonl")

    # output name: slice_TAG_repaired_TIMESTAMP.warc.gz
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out_warc_name = f"slice_{version_tag}_repaired_{ts}.warc.gz"
    out_warc_path = os.path.join(version_dir, "warc", out_warc_name)
    out_index_path = os.path.join(version_dir, f"index_repaired_{ts}.jsonl")

    os.makedirs(os.path.dirname(out_warc_path), exist_ok=True)

    bad_stores = set(broken_store_files)

    print(f"[Rebuild] Target Slice: {bad_slice_filename}")
    print(f"[Rebuild] Blacklisted Stores: {len(bad_stores)} files")
    print(f"[Rebuild] Generating Member-GZIP WARC at: {out_warc_path}")

    warc_fp = open(out_warc_path, "wb")
    # key point: member-gzip mode, each record compressed independently
    writer = WARCWriter(warc_fp, gzip=True)
    index_fp = open(out_index_path, "w", encoding="utf-8")

    count = 0
    skipped_broken_store = 0
    skipped_invalid_ref = 0
    skipped_other_warc = 0

    with open(original_index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 1. is this record part of the slice warc being repaired?
            current_warc = row.get("warc")
            if current_warc != bad_slice_filename:
                # double-check even records from other slices;
                # if auto_doctor.py did its job this should not hit
                store_ref = row.get("store_ref") or {}
                if store_ref.get("warc") in bad_stores:
                    skipped_broken_store += 1
                    continue

                # normal record from another file: keep as-is
                index_fp.write(line + "\n")
                skipped_other_warc += 1
                continue

            # === below: records belonging to bad_slice_filename ===

            # 2. integrity check: is the store ref valid?
            store_ref = row.get("store_ref")
            if not store_ref or not isinstance(store_ref, dict):
                skipped_invalid_ref += 1
                continue

            store_warc_name = store_ref.get("warc")
            store_record_id = store_ref.get("record_id")

            if not store_warc_name or not store_record_id:
                skipped_invalid_ref += 1
                continue

            # 3. does it reference a broken store? (second filter)
            if store_warc_name in bad_stores:
                skipped_broken_store += 1
                continue

            # 4. rebuild the WARC record
            url = row.get("url")

            # always backfill captured_at so index and warc header agree
            captured_at = row.get("captured_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            row["captured_at"] = captured_at

            # build headers
            warc_headers = [
                ("WARC-Refers-To",            store_record_id),
                ("WARC-Refers-To-Target-URI", url),
                ("WARC-Refers-To-Date",       captured_at),
                ("X-AS2Web-Version-Tag",      version_tag),
                ("X-AS2Web-Site",             row.get("site")),
                ("X-AS2Web-Seed",             row.get("seed")),
                ("X-AS2Web-Kind",             row.get("kind")),
                ("X-AS2Web-Mime",             row.get("mime")),
                ("X-AS2Web-Store-Warc",       store_warc_name),
                ("X-AS2Web-Store-Record-ID",  store_record_id),
                ("X-AS2Web-Store-Origin",     row.get("store_origin", "reused_from_store")),
            ]
            # drop None values
            warc_headers = [(k, v) for k, v in warc_headers if v is not None]

            record = writer.create_warc_record(
                uri=url,
                record_type="revisit",
                warc_headers_dict=dict(warc_headers),
                payload=None,
                http_headers=None
            )
            writer.write_record(record)

            # 5. update the index record
            new_rid = record.rec_headers.get_header("WARC-Record-ID")
            row["record_id"] = new_rid
            row["warc"] = out_warc_name  # point at the new repaired file

            index_fp.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1

    warc_fp.close()
    index_fp.close()

    print(f"[Rebuild] Done. Migrated: {count}")
    print(f"          Skipped (Bad Store): {skipped_broken_store}")
    print(f"          Skipped (Invalid Ref): {skipped_invalid_ref}")
    print(f"          Passthrough (Other WARCs): {skipped_other_warc}")

    return out_warc_path, out_index_path
