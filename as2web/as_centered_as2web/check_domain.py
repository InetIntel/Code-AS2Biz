#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_domain.py
───────────────
Reads as_centered_as2domain_unchecked.json produced by main_pipeline.py,
probes each unique domain once (global dedup), and writes:
  - {base_dir}/{date}/_domain_probe_cache.json  (rolling cache, resume-safe)
  - {base_dir}/{date}/as_centered_as2domain.json (final per-ASN result)

Improvements over the reference version:
  * --save_every default lowered to 200 (less data lost on crash)
  * 1 automatic retry on transient network errors (timeout / reset)
  * --strip_tried flag (default on) keeps final output file lean;
    full probe detail stays in the cache file
  * --base_dir lets you point at any output root without cd-ing first
"""

import argparse
import json
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

# ---------------------------------------------------------------------------
# HTTP config
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 5.0
HEADERS = {
    # Identify the checker. If you adapt this code, point the +URL at a page
    # that describes your project and gives a contact address.
    "User-Agent": (
        "Mozilla/5.0 (compatible; AS2Web-DomainCheck/1.0; "
        "+https://github.com/InetIntel/Dataset-AS2Web)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

def is_accessible_status(status: int) -> bool:
    return 200 <= (status or 0) < 400

def is_blocked_status(status: int) -> bool:
    # Common "reachable but denied/rate-limited" statuses.
    return (status or 0) in {401, 403, 429}

# ---------------------------------------------------------------------------
# WAF / middlebox detection
# ---------------------------------------------------------------------------
MIDDLEBOX_DOMAINS = [
    "perfdrive.com",
    "incapsula.com",
    "incap_dns",
    "challenges.cloudflare.com",
    "captcha-delivery.com",
    "datadome.co",
    "hcaptcha.com",
    "recaptcha.net",
    "google.com/recaptcha",
    "cookiebot.com",
    "onetrust.com",
    "chromewebdata",
]

def is_middlebox_redirect(final_url: str) -> bool:
    if not final_url:
        return False
    u = final_url.lower()
    return any(bad in u for bad in MIDDLEBOX_DOMAINS)

# ---------------------------------------------------------------------------
# Per-thread session (connection reuse)
# ---------------------------------------------------------------------------
_thread_local = threading.local()

def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.max_redirects = 10
        s.verify = False
        _thread_local.session = s
    return s

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def clean_url(url: str) -> str:
    url = url.strip().lower()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    netloc = parsed.netloc or parsed.path
    path   = parsed.path if parsed.netloc else ""
    path   = path.rstrip("/")
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return f"{netloc}{path}" if netloc else ""

def make_variants(cleaned: str):
    if not cleaned:
        return []
    if "/" in cleaned:
        host, path = cleaned.split("/", 1)
        path = "/" + path
    else:
        host, path = cleaned, ""
    seen, ordered = set(), []
    for u in [
        f"https://{host}{path}",
        f"https://www.{host}{path}",
        f"http://{host}{path}",
        f"http://www.{host}{path}",
    ]:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered

# ---------------------------------------------------------------------------
# HTTP probe (streamed GET, WAF rollback, 1 retry on transient errors)
# ---------------------------------------------------------------------------
_TRANSIENT = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
)

def streamed_get_probe(url: str, timeout: float, retries: int = 1):
    to   = (timeout, timeout)
    sess = _session()

    def _check(resp, original_url):
        if is_accessible_status(resp.status_code):
            if is_middlebox_redirect(resp.url):
                return "blocked", resp.status_code, original_url, "waf_detected_reverted"
            return "accessible", resp.status_code, resp.url, ""
        if is_blocked_status(resp.status_code):
            return "blocked", resp.status_code, resp.url, "http_blocked"
        return "unreachable", resp.status_code, resp.url, ""

    last_err = ""
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(1.0)   # brief back-off before retry
        try:
            r = sess.get(url, allow_redirects=True, timeout=to, stream=True)
            try:
                reachability, status, final_u, note = _check(r, url)
            finally:
                # We only need status/headers/final URL. Close early to avoid
                # downloading large bodies while still using GET semantics.
                r.close()
            return reachability, status, final_u, note
        except _TRANSIENT as e:
            last_err = f"{type(e).__name__}: {e}"
            continue     # retry
        except Exception as e:
            return "unreachable", None, None, f"{type(e).__name__}: {e}"

    return "unreachable", None, None, last_err

# ---------------------------------------------------------------------------
# Domain-level probe
# ---------------------------------------------------------------------------
def probe_one_domain(cleaned_domain: str, timeout: float, topk_parallel: int = 1):
    variants = make_variants(cleaned_domain)
    tried    = []
    if not variants:
        return {
            "domain": cleaned_domain, "accessible": False, "reachable_blocked": False,
            "reachability": "unreachable",
            "selected_variant": None, "final_full_url": None,
            "status": None, "tried": tried,
        }

    def run_sequential(var_list):
        blocked_result = None
        for full in var_list:
            reachability, status, final_url, err = streamed_get_probe(full, timeout)
            ok = (reachability == "accessible")
            blocked = (reachability == "blocked")
            tried.append({
                "url": full,
                "ok": ok,
                "reachable_blocked": blocked,
                "reachability": reachability,
                "status": status,
                          "final_url": final_url, "err": err})
            if ok:
                return {
                    "domain": cleaned_domain, "accessible": True, "reachable_blocked": False,
                    "reachability": "accessible",
                    "selected_variant": full, "final_full_url": final_url or full,
                    "status": status, "tried": tried,
                }
            if blocked and blocked_result is None:
                blocked_result = {
                    "domain": cleaned_domain, "accessible": False, "reachable_blocked": True,
                    "reachability": "blocked",
                    "selected_variant": full, "final_full_url": final_url or full,
                    "status": status,
                }
        if blocked_result:
            blocked_result["tried"] = tried
            return blocked_result
        return None

    if topk_parallel <= 1:
        res = run_sequential(variants)
        if res:
            return res
    else:
        first, rest = variants[:topk_parallel], variants[topk_parallel:]
        results      = {}
        tried_par    = []
        success      = None

        with ThreadPoolExecutor(max_workers=topk_parallel) as ex:
            fut2url = {ex.submit(streamed_get_probe, v, timeout): v for v in first}
            for fut in as_completed(fut2url):
                v = fut2url[fut]
                try:
                    reachability, status, final_url, err = fut.result()
                except Exception as e:
                    reachability, status, final_url, err = "unreachable", None, None, f"future_error: {e}"
                ok = (reachability == "accessible")
                blocked = (reachability == "blocked")
                results[v] = (reachability, status, final_url, err)
                tried_par.append({"url": v, "ok": ok, "reachable_blocked": blocked,
                                   "reachability": reachability, "status": status,
                                   "final_url": final_url, "err": err})
                if success is None:
                    if reachability in ("accessible", "blocked"):
                        success = v
                else:
                    old_reach = results[success][0]
                    old_rank = 2 if old_reach == "accessible" else 1
                    new_rank = 2 if reachability == "accessible" else (1 if reachability == "blocked" else 0)
                    if new_rank > old_rank or (new_rank == old_rank and variants.index(v) < variants.index(success)):
                        success = v

        tried_par.sort(key=lambda x: variants.index(x["url"]))
        tried.extend(tried_par)

        if success:
            reachability, status, final_url, _ = results[success]
            if reachability == "accessible":
                return {
                    "domain": cleaned_domain, "accessible": True, "reachable_blocked": False,
                    "reachability": "accessible",
                    "selected_variant": success, "final_full_url": final_url or success,
                    "status": status, "tried": tried,
                }
            blocked_candidate = {
                "domain": cleaned_domain, "accessible": False, "reachable_blocked": True,
                "reachability": "blocked",
                "selected_variant": success, "final_full_url": final_url or success,
                "status": status, "tried": tried,
            }
            # Keep probing remaining variants to prefer an actually accessible
            # endpoint if one exists after a blocked top-k result.
            if rest:
                res = run_sequential(rest)
                if res and res.get("accessible"):
                    return res
                if res and res.get("reachable_blocked"):
                    return blocked_candidate
            return {
                "domain": cleaned_domain, "accessible": False, "reachable_blocked": True,
                "reachability": "blocked",
                "selected_variant": success, "final_full_url": final_url or success,
                "status": status, "tried": tried,
            }
        if rest:
            res = run_sequential(rest)
            if res:
                return res

    return {
        "domain": cleaned_domain, "accessible": False, "reachable_blocked": False,
        "reachability": "unreachable",
        "selected_variant": None, "final_full_url": None,
        "status": None, "tried": tried,
    }

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def dump_json_atomic(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Check accessible website per ASN with global-domain dedup."
    )
    ap.add_argument("--date",          required=True,       help="Date in YYYYMMDD.")
    ap.add_argument("--base-dir", "--base_dir", dest="base_dir", required=True,
                    help="Root directory holding per-date input and output folders.")
    ap.add_argument("--workers",       type=int,   default=32)
    ap.add_argument("--timeout",       type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--topk_parallel", type=int,   default=1,
                    help="Probe top-K variants in parallel per domain (1 = sequential, safest).")
    ap.add_argument("--limit",         type=int,   default=0,
                    help="Limit ASN count (0 = no limit, useful for testing).")
    ap.add_argument("--progress_every",type=int,   default=100)
    ap.add_argument("--save_every",    type=int,   default=200,
                    help="Flush domain cache every N probed domains.")
    ap.add_argument("--no_resume",     action="store_true",
                    help="Ignore existing cache and re-probe everything.")
    ap.add_argument("--keep_tried",    action="store_true",
                    help="Include full 'tried' list in final output (makes file large).")
    args = ap.parse_args()

    date     = args.date
    base_dir = Path(args.base_dir)
    date_dir = base_dir / date

    input_json   = date_dir / "as_centered_as2domain_unchecked.json"
    cache_json   = date_dir / "_domain_probe_cache.json"
    out_json     = date_dir / "as_centered_as2domain.json"

    # ── Load input ───────────────────────────────────────────────────────────
    logging.info(f"Loading {input_json} ...")
    try:
        as2domain_list = load_json(input_json)
    except FileNotFoundError:
        logging.error(f"Input file not found: {input_json}")
        raise SystemExit(1) from None

    all_asns = list(as2domain_list.keys())
    if args.limit > 0:
        all_asns = all_asns[:args.limit]
        logging.info(f"Limiting to first {args.limit} ASNs.")

    # ── Build per-ASN cleaned-domain lists + global unique set ──────────────
    asn_to_cleaned       = {}
    asn_clean_to_original = {}
    unique_domains_ordered = []
    seen_global = set()

    for asn in all_asns:
        cleaned_list = []
        seen_local   = set()
        orig_map     = {}
        for u in (as2domain_list.get(asn) or []):
            cu = clean_url(u)
            if not cu or cu in seen_local:
                continue
            seen_local.add(cu)
            cleaned_list.append(cu)
            orig_map.setdefault(cu, u)
            if cu not in seen_global:
                seen_global.add(cu)
                unique_domains_ordered.append(cu)
        asn_to_cleaned[asn]        = cleaned_list
        asn_clean_to_original[asn] = orig_map

    logging.info(f"Total ASNs: {len(all_asns)} | Unique cleaned domains: {len(unique_domains_ordered)}")

    # ── Load cache (resume) ──────────────────────────────────────────────────
    domain_results = {}
    if not args.no_resume and cache_json.exists():
        try:
            domain_results = load_json(cache_json)
            logging.info(f"Resumed from cache: {len(domain_results)} domain entries.")
        except Exception as e:
            logging.warning(f"Could not load cache {cache_json}: {e}")

    to_probe = [d for d in unique_domains_ordered if d not in domain_results]
    logging.info(f"Domains remaining to probe: {len(to_probe)}")

    # ── Probe ────────────────────────────────────────────────────────────────
    def save_cache():
        dump_json_atomic(domain_results, cache_json)
        logging.info(f"[Cache] Saved {len(domain_results)} domain results to {cache_json}")

    done = 0
    if to_probe:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            fut2dom = {
                ex.submit(probe_one_domain, d, args.timeout, args.topk_parallel): d
                for d in to_probe
            }
            for fut in as_completed(fut2dom):
                d = fut2dom[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {
                        "domain": d, "accessible": False, "reachable_blocked": False,
                        "reachability": "unreachable",
                        "selected_variant": None, "final_full_url": None,
                        "status": None, "tried": [], "error": str(e),
                    }
                domain_results[d] = res
                done += 1
                if done % args.progress_every == 0:
                    mark = "✓" if res["accessible"] else "✗"
                    logging.info(f"[Probe] {done}/{len(to_probe)} | {d} {mark}")
                if done % args.save_every == 0:
                    save_cache()

        save_cache()

    # ── Back-fill per-ASN results ────────────────────────────────────────────
    asn_results = {}
    ok_cnt = 0
    blocked_cnt = 0

    for asn, clist in asn_to_cleaned.items():
        chosen_accessible = None
        chosen_blocked = None
        for d in clist:
            r = domain_results.get(d)
            if not r:
                continue
            if r.get("accessible"):
                chosen_accessible = {
                    "asn":              asn,
                    "accessible":       True,
                    "reachable_blocked": False,
                    "reachability":     "accessible",
                    "chosen_clean":     d,
                    "chosen_original":  asn_clean_to_original.get(asn, {}).get(d, d),
                    "selected_variant": r.get("selected_variant"),
                    "final_full_url":   r.get("final_full_url"),
                    "status":           r.get("status"),
                }
                if args.keep_tried:
                    chosen_accessible["tried"] = r.get("tried", [])
                break
            if r.get("reachable_blocked") and chosen_blocked is None:
                chosen_blocked = {
                    "asn":               asn,
                    "accessible":        False,
                    "reachable_blocked": True,
                    "reachability":      "blocked",
                    "chosen_clean":      d,
                    "chosen_original":   asn_clean_to_original.get(asn, {}).get(d, d),
                    "selected_variant":  r.get("selected_variant"),
                    "final_full_url":    r.get("final_full_url"),
                    "status":            r.get("status"),
                }
                if args.keep_tried:
                    chosen_blocked["tried"] = r.get("tried", [])

        if chosen_accessible:
            asn_results[asn] = chosen_accessible
            ok_cnt += 1
        elif chosen_blocked:
            asn_results[asn] = chosen_blocked
            blocked_cnt += 1
        else:
            asn_results[asn] = {
                "asn":             asn,
                "accessible":      False,
                "reachable_blocked": False,
                "reachability":    "unreachable",
                "fallback_domains": list(as2domain_list.get(asn) or []),
            }

    dump_json_atomic(asn_results, out_json)
    logging.info(
        f"Done. Accessible: {ok_cnt}/{len(all_asns)} ASNs | "
        f"Reachable-but-blocked: {blocked_cnt}/{len(all_asns)} ASNs. "
        f"Output: {out_json}"
    )

if __name__ == "__main__":
    main()
