#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import csv
import sys
import time
import logging
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ------- logging -------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

# ------- similarity backend -------
try:
    from rapidfuzz import fuzz as rf_fuzz
    def partial_ratio(a, b): return rf_fuzz.partial_ratio(a, b)
except Exception:
    logging.error("rapidfuzz is not available. Please `pip install rapidfuzz`.")
    sys.exit(1)

# ------- HTTP config -------
DEFAULT_TIMEOUT = 4.0
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AS2Web/1.0)"}
OK_STATUS = set(range(200, 400))

# ------- core utils -------
def clean_url(url: str) -> str:
    """Normalize a URL string to 'host' or 'host/path' (lowercase, strip scheme, strip leading www., strip trailing slash)."""
    url = url.strip().lower()
    parsed = urlparse(url)
    netloc = parsed.netloc or parsed.path  # handle raw 'example.com' without scheme
    path = parsed.path if parsed.netloc else ""
    path = path.rstrip("/")
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return f"{netloc}{path}" if path else netloc

def make_variants(cleaned: str):
    """Generate full URL variants to try, in order: https -> https+www -> http -> http+www."""
    if "/" in cleaned:
        host, path = cleaned.split("/", 1)
        path = "/" + path
    else:
        host, path = cleaned, ""
    cands = [
        f"https://{host}{path}",
        f"https://www.{host}{path}",
        f"http://{host}{path}",
        f"http://www.{host}{path}",
    ]
    # stable de-dup
    seen, ordered = set(), []
    for u in cands:
        if u not in seen:
            seen.add(u); ordered.append(u)
    return ordered

def head_then_get(url: str, timeout: float):
    """HEAD first; if 403/405, then GET. Return (ok, status, errstr)."""
    try:
        r = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=timeout)
        if r.status_code in OK_STATUS:
            return True, r.status_code, ""
        if r.status_code in (403, 405):
            g = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=timeout)
            return (g.status_code in OK_STATUS), g.status_code, ""
        return False, r.status_code, ""
    except requests.RequestException as e:
        return False, None, str(e)

def check_access_one(cleaned: str, timeout: float, topk_parallel: int = 1):
    """
    Try URL variants (https, https+www, http, http+www) for a cleaned key.
    If topk_parallel > 1, probe the first K variants concurrently and pick earliest success (tie by order).
    Return dict: { 'accessible', 'final_full_url', 'status': int|None, 'tried': [ {url, ok, status, err} ] }
    """
    variants = make_variants(cleaned)
    tried = []

    # Sequential fast-path if topk_parallel == 1
    if topk_parallel <= 1:
        for full in variants:
            ok, status, err = head_then_get(full, timeout)
            tried.append({"url": full, "ok": ok, "status": status, "err": err})
            if ok:
                return {"accessible": True, "final_full_url": full, "status": status, "tried": tried}
        return {"accessible": False, "final_full_url": None, "status": None, "tried": tried}

    # Parallel only for first K variants; fall back sequentially for the rest
    first = variants[:topk_parallel]
    rest = variants[topk_parallel:]

    with ThreadPoolExecutor(max_workers=topk_parallel) as ex:
        fut2url = {ex.submit(head_then_get, v, timeout): v for v in first}
        success_full = None
        results = {}
        for fut in as_completed(fut2url):
            v = fut2url[fut]
            ok, status, err = fut.result()
            results[v] = (ok, status, err)
            tried.append({"url": v, "ok": ok, "status": status, "err": err})
            if ok and success_full is None:
                success_full = v
        if success_full is not None:
            tried_sorted = sorted(tried, key=lambda x: variants.index(x["url"]))
            return {"accessible": True, "final_full_url": success_full, "status": results[success_full][1], "tried": tried_sorted}

    for v in rest:
        ok, status, err = head_then_get(v, timeout)
        tried.append({"url": v, "ok": ok, "status": status, "err": err})
        if ok:
            return {"accessible": True, "final_full_url": v, "status": status, "tried": tried}

    return {"accessible": False, "final_full_url": None, "status": None, "tried": tried}

def similarity_score(url_cleaned: str, as_name: str, org_name: str):
    a = (as_name or "").lower()
    o = (org_name or "").lower()
    u = (url_cleaned or "").lower()
    as_sim  = partial_ratio(u, a) if a else 0
    org_sim = partial_ratio(u, o) if o else 0
    return 0.5 * as_sim + 0.5 * org_sim

def choose_url_for_asn(asn: str, raw_urls, as_name: str, org_name: str, timeout: float, topk_parallel: int):
    """
    For a single ASN: clean URLs -> score -> sort desc -> test accessibility until one works.
    Returns a detailed dict.
    """
    cleaned_set = {clean_url(u) for u in raw_urls if u}
    if not cleaned_set:
        return {"asn": asn, "chosen_clean": None, "score": 0.0, "accessible": False,
                "final_full_url": None, "status": None, "candidates": []}

    # (url_cleaned, score)
    scored = [(u, similarity_score(u, as_name, org_name)) for u in cleaned_set]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Try by order, each with (possibly parallel) variant probes
    for u, sc in scored:
        chk = check_access_one(u, timeout=timeout, topk_parallel=topk_parallel)
        if chk["accessible"]:
            return {
                "asn": asn,
                "chosen_clean": u,
                "score": sc,
                "accessible": True,
                "final_full_url": chk["final_full_url"],
                "status": chk["status"],
                "candidates": [{"url_clean": uu, "score": ss} for uu, ss in scored],
                "tried": chk["tried"],
            }

    # None accessible; pick highest score as fallback
    u0, sc0 = scored[0]
    chk0 = check_access_one(u0, timeout=timeout, topk_parallel=1)  # already tried, but keep structure
    return {
        "asn": asn,
        "chosen_clean": u0,
        "score": sc0,
        "accessible": False,
        "final_full_url": None,
        "status": None,
        "candidates": [{"url_clean": uu, "score": ss} for uu, ss in scored],
        "tried": chk0["tried"],
    }

# ------- I/O helpers -------
def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def dump_json_atomic(obj, path: Path):
    """Atomic write: write to .tmp then rename, so an interruption cannot corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

def dump_csv(rows, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["asn", "chosen_clean", "final_full_url", "score", "status", "accessible"])
        for r in rows:
            w.writerow([r["asn"], r["chosen_clean"], r["final_full_url"], f"{r['score']:.1f}", r["status"], r["accessible"]])

# ------- main -------
def main():
    ap = argparse.ArgumentParser(description="Pick most relevant & accessible website per ASN (parallel).")
    ap.add_argument("--date", required=True, help="Date in YYYYMMDD.")
    ap.add_argument("--input-dir", required=True, help="Root containing date-stamped collector outputs.")
    ap.add_argument("--output-dir", required=True, help="Root for date-stamped processed outputs.")
    ap.add_argument("--workers", type=int, default=16, help="Thread workers for per-ASN parallelism.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout per request (seconds).")
    ap.add_argument("--topk_parallel", type=int, default=3, help="Parallel probe top-K variants per candidate (1 = sequential).")
    ap.add_argument("--limit", type=int, default=0, help="Optional: process first N ASNs (for quick test).")
    ap.add_argument("--progress_every", type=int, default=10, help="Print progress every N ASNs.")
    ap.add_argument("--save_every", type=int, default=50, help="Checkpoint every N ASNs.")
    ap.add_argument("--resume", action="store_true", default=True, help="Resume from out_json if it exists (on by default).")
    args = ap.parse_args()

    date = args.date
    pdb_path = Path(args.input_dir) / date / f"{date}_pdb_as2url_raw.json"
    info_path = Path(args.input_dir) / date / f"{date}_pdb_needed_as_info.json"
    out_json = Path(args.output_dir) / date / f"{date}_pdb_as2url.json"

    logging.info("Loading inputs...")
    pdb_as2web_raw = load_json(pdb_path)   # {asn: [url, ...]}
    asn_info       = load_json(info_path)  # {asn: {"AS Name": "...", "Org Name": "..."}}

    # build the work queue & resume
    all_asns = list(pdb_as2web_raw.keys())
    if args.limit and args.limit > 0:
        all_asns = all_asns[:args.limit]
        logging.info(f"Limiting to first {args.limit} ASNs.")

    existing = {}
    if args.resume and out_json.exists():
        try:
            existing = load_json(out_json)
            logging.info(f"Resume enabled: loaded {len(existing)} existing results from {out_json}.")
        except Exception as e:
            logging.warning(f"Failed to load existing {out_json}: {e}")

    remaining_asns = [a for a in all_asns if a not in existing]
    logging.info(f"Total ASNs in scope: {len(all_asns)} | Remaining: {len(remaining_asns)}")

    t0 = time.time()
    new_results_map = {}  # {asn: result}
    done_this_run = 0

    # save helper (merges existing + new results)
    def save_now():
        merged = dict(existing)
        merged.update(new_results_map)
        dump_json_atomic(merged, out_json)
        logging.info(f"Saved checkpoint: {len(merged)} total results to {out_json}")

    # main loop
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = []
            for asn in remaining_asns:
                raw_urls = pdb_as2web_raw.get(asn, []) or []
                info = asn_info.get(asn, {})
                as_name = info.get("AS Name", "") or info.get("ASName", "")
                org_name = info.get("Org Name", "") or info.get("org", "") or info.get("descr", "")
                fut = ex.submit(
                    choose_url_for_asn,
                    asn, raw_urls, as_name, org_name, args.timeout, args.topk_parallel
                )
                futs.append(fut)

            for fut in as_completed(futs):
                res = fut.result()
                new_results_map[res["asn"]] = res
                done_this_run += 1

                # progress output
                if done_this_run % args.progress_every == 0:
                    accessible_mark = "✓" if res["accessible"] else "✗"
                    logging.info(
                        f"[Progress] Done {done_this_run}/{len(remaining_asns)} "
                        f"(overall {len(existing)+done_this_run}/{len(all_asns)}) | "
                        f"Last: ASN {res['asn']} | score={res['score']:.1f} | "
                        f"access={accessible_mark} | url={res.get('final_full_url')} | status={res.get('status')}"
                    )

                # periodic save
                if done_this_run % args.save_every == 0:
                    save_now()

        # final save once everything is done
        save_now()

    finally:
        # also save once on exception / Ctrl+C
        if new_results_map:
            save_now()

    dt = time.time() - t0
    merged_total = len(existing) + done_this_run
    ok_cnt = sum(1 for r in {**existing, **new_results_map}.values() if r.get("accessible"))
    logging.info(f"Done in {dt:.1f}s. Accessible picked for {ok_cnt}/{merged_total} ASNs. Output: {out_json}")

if __name__ == "__main__":
    main()
