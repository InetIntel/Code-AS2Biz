#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Progress checker with three-way state:
- DONE:            seed has at least one 'html' entry in version index
- FAILED:          seed appears in scrape_report with a non-success outcome
- NEVER_ATTEMPTED: neither in index nor in scrape_report

Usage:
  # Auto-discover URL list from as2web/<YYYYMMDD>/as2web.json:
  python check_scraper.py --version-tag 2026-03-01 --archives-dir ./archives

  # Explicit URL list:
  python check_scraper.py --version-tag 2025-09-01 --input urls.json --archives-dir ./archives
"""

import argparse
import json
import os
import re
from pathlib import Path
from collections import Counter

import pandas as pd


def ensure_url_with_protocol(u: str) -> str:
    u = (u or "").strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _resolve_as2web_path(version_tag: str) -> str | None:
    tag_compact = version_tag.replace("-", "")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, "as2web", tag_compact, "as2web.json")
    return path if os.path.isfile(path) else None


def load_input_seeds(path: str):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        seeds_raw = raw
    elif isinstance(raw, dict) and "urls" in raw:
        seeds_raw = raw["urls"]
    else:
        raise SystemExit(f"Unsupported JSON format in {path}: expected array or {{'urls': [...]}}")
    seeds_norm = [ensure_url_with_protocol(str(x).strip()) for x in seeds_raw if str(x).strip()]
    seen, seeds = set(), []
    for s in seeds_norm:
        if s not in seen:
            seen.add(s); seeds.append(s)
    return seeds


def load_seeds_from_as2web(path: str):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    data = obj.get("data")
    if not isinstance(data, dict):
        raise SystemExit(f"as2web.json at {path} has no valid 'data' dict")
    seen = set()
    seeds = []
    for asn_entry in data.values():
        if not isinstance(asn_entry, dict):
            continue
        raw = (asn_entry.get("url") or "").strip()
        if not raw:
            continue
        normed = ensure_url_with_protocol(raw)
        if normed not in seen:
            seen.add(normed)
            seeds.append(normed)
    seeds.sort()
    return seeds


def _display_df(name: str, df: pd.DataFrame):
    out_dir = Path("./check")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    out_path = out_dir / f"{safe}.csv"
    df.to_csv(out_path, index=False)
    print(f"[saved] {name} -> {out_path}")


def read_version_index(ver_index_path: Path):
    captured_counts = Counter()
    with open(ver_index_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("kind") != "html":
                continue
            seed = obj.get("seed")
            if not seed:
                continue
            captured_counts[seed] += 1
    return captured_counts


def _find_report_paths(base_dir: Path) -> list[Path]:
    cand = [
        base_dir / "cache" / "scrape_report.jsonl",
        base_dir / "cache" / "scrape_report.json",
        base_dir / "scrape_report.jsonl",
        base_dir / "scrape_report.json",
    ]
    out = [p for p in cand if p.exists()]
    if out:
        return out
    found_jsonl = list(base_dir.rglob("scrape_report.jsonl"))
    found_json  = list(base_dir.rglob("scrape_report.json"))
    return found_jsonl + found_json


def read_scrape_report(report_path: Path):
    """
    Build map: seed -> {status, reason}.
    For seeds with multiple entries (retries), keep the *last* entry
    so the final outcome wins.
    """
    result = {}

    def _ingest(row):
        seed = row.get("seed") or row.get("url")
        if not seed:
            return
        seed = ensure_url_with_protocol(str(seed).strip())
        status = str(row.get("status", "")).lower()
        reason = row.get("reason") or row.get("details") or ""
        result[seed] = {"status": status, "reason": str(reason)}

    try:
        if report_path.suffix == ".jsonl":
            with open(report_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        _ingest(json.loads(line))
                    except Exception:
                        pass
        else:
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for e in (data if isinstance(data, list) else []):
                try:
                    _ingest(e)
                except Exception:
                    pass
    except Exception:
        pass
    return result


def build_progress(archives_dir: str, version_tag: str, seeds: list[str]):
    ver_dir = Path(archives_dir) / "versions" / version_tag
    ver_index = ver_dir / "index.jsonl"
    if not ver_index.exists():
        raise SystemExit(f"Version index not found: {ver_index}")

    captured_counts = read_version_index(ver_index)

    report_paths = _find_report_paths(ver_dir)
    report_map = {}
    for rp in report_paths:
        m = read_scrape_report(rp)
        for k, v in m.items():
            report_map[k] = v

    rows = []
    for s in seeds:
        html_cnt = captured_counts.get(s, 0)
        if html_cnt > 0:
            state = "DONE"
            reason = ""
        else:
            r = report_map.get(s)
            if r:
                st = r["status"]
                if st == "success":
                    state = "DONE"
                elif st == "skipped":
                    state = "SKIPPED"
                elif st in ("failed", "error", "exception"):
                    state = "FAILED"
                else:
                    state = "ATTEMPTED"
                reason = r.get("reason", "")
            else:
                state = "NEVER_ATTEMPTED"
                reason = ""
        rows.append({
            "seed": s,
            "state": state,
            "html_entries": html_cnt,
            "last_reason": reason,
        })

    df = pd.DataFrame(rows)

    total = len(df)
    done = int((df["state"] == "DONE").sum())
    skipped = int((df["state"] == "SKIPPED").sum())
    failed = int((df["state"] == "FAILED").sum())
    attempted = int((df["state"] == "ATTEMPTED").sum())
    never = int((df["state"] == "NEVER_ATTEMPTED").sum())

    state_order = pd.CategoricalDtype(
        categories=["DONE", "SKIPPED", "FAILED", "ATTEMPTED", "NEVER_ATTEMPTED"],
        ordered=True,
    )
    df["state"] = df["state"].astype(state_order)
    df = df.sort_values(["state", "seed"])

    _display_df("Per-seed scrape state", df)

    seeds_set = set(seeds)
    extra_captured = [
        (k, v) for k, v in captured_counts.items()
        if k not in seeds_set
    ]
    if extra_captured:
        extra_df = pd.DataFrame(extra_captured, columns=["seed", "html_entries"]).sort_values("html_entries", ascending=False)
        print(f"[info] {len(extra_df)} captured seeds not in input list (redirects, subpages, etc.)", flush=True)

    print(f"\nVersion tag: {version_tag}", flush=True)
    print(f"Targets total: {total}", flush=True)
    print(f"  DONE:            {done}", flush=True)
    print(f"  SKIPPED:         {skipped}", flush=True)
    print(f"  FAILED:          {failed}", flush=True)
    print(f"  ATTEMPTED:       {attempted}", flush=True)
    print(f"  NEVER_ATTEMPTED: {never}", flush=True)

    return df


def main():
    ap = argparse.ArgumentParser(description="Progress checker: DONE / SKIPPED / FAILED / NEVER_ATTEMPTED")
    ap.add_argument("--version-tag", required=True)
    ap.add_argument("--input", default=None,
                    help="Path to JSON URL list. If omitted, auto-discovers as2web/<YYYYMMDD>/as2web.json.")
    ap.add_argument("--archives-dir", default="./archives")
    # keep old flag as alias for backward compatibility
    ap.add_argument("--urls-json", default=None, dest="urls_json_legacy",
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    input_path = args.input or args.urls_json_legacy
    if input_path:
        seeds = load_input_seeds(input_path)
        print(f"[input] loaded {len(seeds)} seeds from {input_path}")
    else:
        as2web_path = _resolve_as2web_path(args.version_tag)
        if as2web_path:
            seeds = load_seeds_from_as2web(as2web_path)
            print(f"[input] loaded {len(seeds)} seeds from {as2web_path}")
        else:
            tag_compact = args.version_tag.replace("-", "")
            raise SystemExit(
                f"No --input provided and no default as2web.json found at "
                f"as2web/{tag_compact}/as2web.json. "
                f"Please provide --input <path> to a URL list."
            )

    build_progress(args.archives_dir, args.version_tag, seeds)


if __name__ == "__main__":
    main()
