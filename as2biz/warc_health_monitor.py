#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Incremental WARC health monitor.

- reuses check_single_warc and find_*_warc_files from
  store_warc_health_check.py / slice_warc_health_check.py.
- records each WARC's mtime + size + status at the last check.
- on the next run, skips a WARC whose mtime/size are unchanged and status=ok.

Example:
  python warc_health_monitor.py \
    --archives-dir ./archives \
    --version-tag 2025-09-01 \
    --state-json ./archives/warc_health_state.json
"""

import argparse
import json
import time
import re
from pathlib import Path
from typing import Dict, Any, Tuple, List

from store_warc_health_check import (
    check_single_warc as check_store_warc,
    find_store_warc_files,
)
from slice_warc_health_check import (
    check_single_warc as check_slice_warc,
    find_warc_files as find_slice_warc_files,
)

STATE_VERSION = 1


def load_state(path: Path) -> Dict[str, Any]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if data.get("version") == STATE_VERSION and "files" in data:
                    return data
            except Exception:
                pass
    return {"version": STATE_VERSION, "files": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def file_sig(p: Path) -> Dict[str, float]:
    st = p.stat()
    return {"mtime": st.st_mtime, "size": st.st_size}


def should_check(p: Path, kind: str, state: Dict[str, Any]) -> Tuple[bool, Dict[str, float]]:
    """
    Return (need_check, sig), where sig holds mtime & size.
    If state already has the same sig with status=ok, no recheck is needed.
    """
    sig = file_sig(p)
    key = f"{kind}:{str(p)}"
    rec = state["files"].get(key)
    if rec:
        if (
            rec.get("mtime") == sig["mtime"]
            and rec.get("size") == sig["size"]
            and rec.get("status") == "ok"
        ):
            return False, sig
    return True, sig


def update_state(
    p: Path,
    kind: str,
    state: Dict[str, Any],
    sig: Dict[str, float],
    ok: bool,
    err: str,
) -> None:
    key = f"{kind}:{str(p)}"
    state["files"][key] = {
        "kind": kind,
        "path": str(p),
        "mtime": sig["mtime"],
        "size": sig["size"],
        "status": "ok" if ok else "broken",
        "error": err,
        "checked_at": time.time(),
    }


def find_probably_open_warc(
    warcs: List[Path],
    pattern: re.Pattern,
) -> Path | None:
    """
    Simple heuristic: among files matching pattern, the one with the highest
    index is assumed to be "currently being written" and may be skipped.
    pattern looks like: r"^store_(\\d{5})\\.warc(\\.gz)?$"
    """
    candidates: List[Tuple[int, Path]] = []
    for p in warcs:
        m = pattern.match(p.name)
        if not m:
            continue
        idx = int(m.group(1))
        candidates.append((idx, p))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def main():
    ap = argparse.ArgumentParser(
        description="Incremental WARC health monitor (store + slice)."
    )
    ap.add_argument(
        "--archives-dir",
        required=True,
        help="Root archives directory.",
    )
    ap.add_argument(
        "--version-tag",
        required=True,
        help="Version tag under archives/versions/<tag> for slice WARCs.",
    )
    ap.add_argument(
        "--state-json",
        required=True,
        help="Path to JSON file storing incremental health state.",
    )
    ap.add_argument(
        "--recheck-broken",
        action="store_true",
        help="Also re-check WARCs that were previously marked as broken.",
    )
    ap.add_argument(
        "--include-latest",
        action="store_true",
        help="Also check the latest store/slice WARC normally skipped as 'probably open'.",
    )
    ap.add_argument(
        "--summary-json",
        default=None,
        help="Optional path to write machine-readable summary for this run.",
    )

    args = ap.parse_args()

    archives_dir = Path(args.archives_dir)
    version_tag = args.version_tag
    state_path = Path(args.state_json)

    store_warc_dir = archives_dir / "store" / "warc"
    slice_warc_dir = archives_dir / "versions" / version_tag / "warc"

    state = load_state(state_path)

    # ==== store WARCs ====
    print(f"[store] scanning {store_warc_dir}")
    store_warcs = find_store_warc_files(store_warc_dir) if store_warc_dir.exists() else []
    store_open_hint = find_probably_open_warc(
        store_warcs, re.compile(r"^store_(\d{5})\.warc(\.gz)?$")
    )

    broken_store: List[str] = []
    checked_store = 0

    for p in store_warcs:
        if store_open_hint and p == store_open_hint and not args.include_latest:
            # may be the WARC currently being written; avoid reading a half gzip
            print(f"[store] skip (probably open): {p.name}")
            continue

        need, sig = should_check(p, "store", state)
        if not need and not args.recheck_broken:
            print(f"[store] skip (unchanged & healthy): {p.name}")
            continue

        print(f"[store] checking: {p.name}")
        ok, err = check_store_warc(p)
        update_state(p, "store", state, sig, ok, err)
        checked_store += 1
        if not ok:
            broken_store.append(f"{p.name}: {err}")

    # ==== slice WARCs ====
    print(f"[slice] scanning {slice_warc_dir}")
    slice_warcs = find_slice_warc_files(slice_warc_dir) if slice_warc_dir.exists() else []
    slice_open_hint = find_probably_open_warc(
        slice_warcs,
        re.compile(rf"^slice_{re.escape(version_tag)}_(\d{{5}})\.warc(\.gz)?$"),
    )

    broken_slice: List[str] = []
    checked_slice = 0

    for p in slice_warcs:
        if slice_open_hint and p == slice_open_hint and not args.include_latest:
            print(f"[slice] skip (probably open): {p.name}")
            continue

        need, sig = should_check(p, "slice", state)
        if not need and not args.recheck_broken:
            print(f"[slice] skip (unchanged & healthy): {p.name}")
            continue

        print(f"[slice] checking: {p.name}")
        ok, err = check_slice_warc(p)
        update_state(p, "slice", state, sig, ok, err)
        checked_slice += 1
        if not ok:
            broken_slice.append(f"{p.name}: {err}")

    save_state(state_path, state)

    print()
    print(f"[summary] store checked: {checked_store}, slice checked: {checked_slice}")
    if broken_store or broken_slice:
        print("[summary] broken WARCs detected:")
        for line in broken_store:
            print("  [store] ", line)
        for line in broken_slice:
            print("  [slice] ", line)
    else:
        print("[summary] no broken WARCs found (this run).")

    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "version_tag": version_tag,
            "checked_store": checked_store,
            "checked_slice": checked_slice,
            "broken_store": broken_store,
            "broken_slice": broken_slice,
            "state_json": str(state_path),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = summary_path.with_suffix(summary_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        tmp.replace(summary_path)
        print(f"[summary] wrote {summary_path}")


if __name__ == "__main__":
    main()
