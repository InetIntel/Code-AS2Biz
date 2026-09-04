#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_search/main.py
──────────────────
Identifies ASNs that still lack an accessible website after the
as_centered_as2web + check_domain pipeline, then asks the OpenAI
web-search API to find an
official page for each organisation.

Two categories of target ASNs:
  inaccessible – present in as_centered_as2domain.json but accessible=False
  no_domain    – in the delegation file but absent from as_centered_as2domain.json

Only ASNs that have an entry in as2orgname.json are queried.

By default, only ASNs listed in final_as_scope.json (from as_centered_as2web)
are considered. Pass --no_scope to include all delegation/as2domain targets;
the run then logs how many extra ASNs and unique org+cc prompts are outside
that scope.

Outputs
-------
  {base_dir}/{date}/_web_search_cache.json.gz   rolling gzip cache (resume-safe)
  {base_dir}/{date}/as2web_from_search.json     final per-ASN results
"""

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Load .env from the script's own directory (no external deps needed)
# ---------------------------------------------------------------------------
def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

_load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"

_FILE_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = """\
You are given an organization's registered name and its ISO 2-letter country code.
Task: find the official website OR an official social media page for this organization.

1. Use web search. Look for the page that clearly corresponds to this specific
   organization. It is registered in the country given by {cc_code}; use that to
   disambiguate organizations that share a name.
2. Prefer an official website. If none is clear, fall back to an official
   LinkedIn, Facebook, X, or other major social media page.
3. Treat a URL as a valid match only if BOTH hold:
   - The page's title or prominent text contains the full registered name or a
     very close variant (e.g., punctuation/case differences, 'Ltd.' vs 'Limited'); and
   - The page's contact/address/location information is consistent with {cc_code},
     OR the page clearly belongs to a multinational company (or its parent/group)
     whose own stated operating footprint includes {cc_code}.
   If the only site you can find belongs to a same-named organization located in a
   different country, with no evidence its footprint covers {cc_code}, respond with `No match.`
4. Do not return third-party internet-registry, whois, BGP/ASN-lookup, or
   network-info aggregator pages (for example PeeringDB, bgp.he.net, bgp.tools,
   RIPE / ARIN / APNIC / LACNIC / AFRINIC, ipinfo.io). Those are not the
   organization's own site; if that is all you can find, respond with `No match.`
5. If two or more organizations plausibly match and you cannot confidently
   identify the right one, respond with `No match.`
6. If you cannot find any plausible official website or page, respond with `No match.`

Output only one of:
- the single best URL, or
- `No match.`

Organization input: {org} {cc_code}
"""

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json_atomic(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_gz_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        log.warning(f"Could not load cache {path}: {e}")
        return {}


def save_gz_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)


def save_gz_json_safe(path: Path, obj: dict):
    with _FILE_LOCK:
        save_gz_json(path, obj)


# ---------------------------------------------------------------------------
# Previous-snapshot reuse helpers
# ---------------------------------------------------------------------------

_RIRS = ("arin", "ripe", "apnic", "afrinic", "lacnic")


def load_whois_identity(whois_base: Path, date_str: str) -> dict:
    """
    Build {asn_str: (as_name, org, descr)} from all RIR _info.json files.
    """
    identity = {}
    for rir in _RIRS:
        info_path = whois_base / rir / date_str / f"{rir}_info.json"
        if not info_path.exists():
            log.warning(f"[reuse] Whois info not found: {info_path}")
            continue
        try:
            raw = load_json(info_path)
        except Exception as e:
            log.warning(f"[reuse] Failed to load {info_path}: {e}")
            continue

        for key, entry in raw.items():
            asn_str = str(key).lstrip("ASas")
            if not asn_str.isdigit():
                continue
            if rir == "arin":
                as_name = entry.get("ASName", "")
                org = entry.get("org", "")
                descr = ""
            elif rir == "lacnic":
                as_name = ""
                org = entry if isinstance(entry, str) else ""
                descr = ""
            else:
                as_name = entry.get("as-name", "")
                org = entry.get("org", "")
                descr = entry.get("descr", "")
            identity[asn_str] = (as_name, org, descr)
    return identity


def detect_prev_date(combined_dir: Path, current_date: str) -> Optional[str]:
    """
    Scan combined_dir for YYYYMMDD folders and return the most recent one
    strictly before current_date, or None.
    """
    import re as _re
    candidates = []
    if not combined_dir.is_dir():
        return None
    for child in combined_dir.iterdir():
        if child.is_dir() and _re.fullmatch(r"\d{8}", child.name):
            if child.name < current_date:
                candidates.append(child.name)
    return max(candidates) if candidates else None


import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_REUSE_HEADERS = {
    # Identify the fetcher. If you adapt this code, point the +URL at a page
    # that describes your project and gives a contact address.
    "User-Agent": (
        "Mozilla/5.0 (compatible; AS2Web-Bot/1.0; "
        "+https://github.com/InetIntel/Dataset-AS2Web)"
    ),
}
_reuse_thread_local = threading.local()

_TRANSIENT_ERRORS = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
)


def _reuse_session() -> requests.Session:
    s = getattr(_reuse_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_REUSE_HEADERS)
        s.max_redirects = 10
        s.verify = False
        _reuse_thread_local.session = s
    return s


def check_url_reachable(url: str, timeout: float = 10.0) -> tuple:
    """
    Returns (reachable: bool, final_url: str | None).
    Reachable = any HTTP response received (even 4xx).
    Unreachable = connection error, DNS failure, or timeout.
    Uses streaming GET with early close (no body download) and one retry.
    """
    sess = _reuse_session()
    to = (timeout, timeout)
    for attempt in range(2):
        if attempt > 0:
            time.sleep(1.0)
        try:
            resp = sess.get(url, allow_redirects=True, timeout=to, stream=True)
            final = resp.url
            resp.close()
            return True, final
        except _TRANSIENT_ERRORS:
            continue
        except Exception:
            return False, None
    return False, None


def check_urls_bulk(url_map: dict, concurrency: int = 32,
                    progress_every: int = 200) -> dict:
    """
    url_map: {asn: url}
    Returns {asn: (reachable, final_url)}.
    """
    results = {}
    total = len(url_map)
    done = 0
    reachable_cnt = 0

    def _check(asn, url):
        reachable, final_url = check_url_reachable(url)
        return asn, reachable, final_url

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_check, asn, url): asn for asn, url in url_map.items()}
        for fu in as_completed(futs):
            try:
                asn, reachable, final_url = fu.result()
                results[asn] = (reachable, final_url)
                if reachable:
                    reachable_cnt += 1
            except Exception as e:
                asn = futs[fu]
                log.warning(f"[reuse] URL check failed for AS{asn}: {e}")
                results[asn] = (False, None)
            done += 1
            if done % progress_every == 0 or done == total:
                log.info(f"[reuse] URL check progress: {done:,}/{total:,}  "
                         f"(reachable so far: {reachable_cnt:,})")
    return results


# ---------------------------------------------------------------------------
# SHA1 cache key (same convention as org_relation_search.py)
# ---------------------------------------------------------------------------

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8"), usedforsecurity=False).hexdigest()

# ---------------------------------------------------------------------------
# OpenAI proxy call (mirrors _web_search_fetch_one_flex)
# ---------------------------------------------------------------------------

def extract_output_text(d: dict) -> str:
    """Extract plain-text content from the API response dict."""
    if not isinstance(d, dict):
        return ""
    if d.get("output_text"):
        return d["output_text"]
    texts = []
    for item in d.get("output", []):
        for c in item.get("content", []):
            if c.get("type") in ("output_text", "text") and "text" in c:
                texts.append(c["text"])
    return "\n".join(texts).strip()


def fetch_one(prompt: str, api_key: str, model: str, max_tokens: int, endpoint: str, retries: int = 3) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":              model,
        "tools":              [{"type": "web_search"}],
        "input":              prompt,
        "max_output_tokens":  max_tokens,
        "reasoning": {"effort": "none"},
        "service_tier":       "flex",
    }
    last_err = None
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(min(30.0, 2.0 * (2 ** attempt)))
        start = time.time()
        try:
            resp    = requests.post(endpoint, json=payload,
                                    headers=headers, timeout=120)
            elapsed = time.time() - start
            status  = resp.status_code

            if status >= 500 or status == 429:
                last_err = f"HTTP {status}: {resp.text[:200]}"
                continue

            ctype = resp.headers.get("Content-Type", "")
            raw   = resp.json() if "application/json" in ctype else {"_non_json": resp.text}

            content = extract_output_text(raw)
            ok      = 200 <= status < 300

            if not ok:
                # Log the body so auth / permission errors are diagnosable
                body_preview = resp.text[:300].strip()
                log.warning(f"[api] HTTP {status} body: {body_preview!r}")

            return {
                "ok":      ok,
                "content": content,
                "error":   None if ok else f"HTTP {status}: {resp.text[:200]}",
                "status":  status,
                "elapsed": elapsed,
            }
        except Exception as e:
            last_err = str(e)
            continue

    return {"ok": False, "content": None, "error": last_err or "unknown",
            "status": None, "elapsed": None}

# ---------------------------------------------------------------------------
# Bulk runner with periodic gzip save (mirrors _run_web_search_bulk)
# ---------------------------------------------------------------------------

def run_bulk(jobs: list, *, api_key: str, cache: dict, cache_path: Path,
             concurrency: int, model: str, max_tokens: int,
             endpoint: str, save_interval: int = 30):
    """
    jobs: list of {key, prompt, meta}
    Results written directly into `cache` dict under each key.
    """
    stop_event = threading.Event()

    def _save_loop():
        while not stop_event.wait(save_interval):
            try:
                save_gz_json_safe(cache_path, cache)
                log.info(f"[cache] periodic save → {cache_path}  ({len(cache)} entries)")
            except Exception as e:
                log.warning(f"[cache] periodic save error: {e}")

    saver = threading.Thread(target=_save_loop, daemon=True)
    saver.start()

    def _one(job: dict):
        key    = job["key"]
        prompt = job["prompt"]

        # check cache under lock (race-safe); treat empty string same as None
        with _FILE_LOCK:
            existing = (cache.get(key) or {}).get("content")
            if existing is not None and existing != "":
                return

        res = fetch_one(prompt, api_key=api_key, model=model, max_tokens=max_tokens, endpoint=endpoint)

        with _FILE_LOCK:
            cache[key] = {
                "prompt":  prompt,
                "content": res.get("content"),
                "ok":      bool(res.get("ok")),
                "error":   res.get("error"),
                "status":  res.get("status"),
                "elapsed": res.get("elapsed"),
                "model":   model,
                "ts":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "meta":    job.get("meta") or {},
            }

        ok_mark = "✓" if res.get("ok") else "✗"
        log.info(f"[api] {ok_mark}  key={key[:8]}  status={res.get('status')}  "
                 f"elapsed={res.get('elapsed'):.1f}s  "
                 f"content={str(res.get('content',''))[:60]!r}")

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_one, j) for j in jobs]
        for fu in as_completed(futs):
            try:
                fu.result()
            except Exception as e:
                log.error(f"[api] task exception: {e}")

    stop_event.set()
    saver.join()

# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_response(content: Optional[str]) -> Optional[str]:
    """
    Return the URL string, or None if the model replied 'No match.'
    Strips surrounding whitespace / markdown formatting.
    """
    if not content:
        return None
    text = content.strip().strip("`").strip()
    if re.match(r"no\s+match", text, re.I):
        return None
    # take the first line in case the model added explanation
    first_line = text.splitlines()[0].strip()
    return first_line or None

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Web-search for official pages of ASNs with no accessible domain."
    )
    ap.add_argument("--date",         required=True,
                    help="Date in YYYYMMDD.")
    ap.add_argument("--input_dir", required=True, help="Directory containing as2orgname.json, asn2cc.json, and final_as_scope.json.")
    ap.add_argument("--as2domain_dir", required=True, help="Directory containing as_centered_as2domain.json.")
    ap.add_argument("--base_dir", required=True, help="Root for per-date output folders.")
    ap.add_argument("--model",        default="gpt-5.2",
                    help="Model to use. Must support the web_search tool.")
    ap.add_argument("--max_tokens",   type=int,   default=512)
    ap.add_argument("--workers",      type=int,   default=8)
    ap.add_argument("--save_interval",type=int,   default=30,
                    help="Seconds between periodic cache saves.")
    ap.add_argument("--max_new",      type=int,   default=0,
                    help="Cap on new API calls (0 = no limit; useful for testing).")
    ap.add_argument("--no_resume",    action="store_true",
                    help="Ignore existing cache and re-query everything.")
    ap.add_argument("--dry_run",      action="store_true",
                    help="Build prompts and report counts without calling the API.")
    ap.add_argument("--whois_dir", default=None,
                    help="Base path for RIR Whois <rir>_info.json files, used only by the "
                         "previous-snapshot reuse heuristic. Optional: if omitted (or the "
                         "files are absent) reuse is skipped and every target ASN is queried "
                         "fresh. See docs/01_inputs.md for the <rir>_info.json schema.")
    ap.add_argument("--endpoint", default=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_ENDPOINT),
                    help="OpenAI-compatible Responses API endpoint.")
    ap.add_argument("--prev_combined", default=None,
                    help="Path to previous snapshot's as2web.json for reuse heuristic. "
                         "Default: auto-detect from ~/as2web/combined/")
    ap.add_argument("--no_reuse",     action="store_true",
                    help="Disable the previous-snapshot reuse heuristic.")
    ap.add_argument(
        "--no_scope",
        action="store_true",
        help="Do not restrict targets to ASNs in final_as_scope.json (treat delegation "
             "and as2domain broadly). Logs extra out-of-scope ASNs, org+cc prompts that "
             "touch them, and how many org+cc prompts would not run if scope were ON.",
    )
    args = ap.parse_args()

    date_str = args.date

    # as2domain dir: where check_domain.py wrote as_centered_as2domain.json
    as2domain_dir = Path(args.as2domain_dir)
    input_dir = Path(args.input_dir)

    # Output dir: writable location owned by the running user
    out_dir    = Path(args.base_dir) / date_str
    cache_path = out_dir / "_web_search_cache.json.gz"
    out_path   = out_dir / "as2web_from_search.json"
    llm_path   = out_dir / "llm_responses.json"

    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"as2domain dir: {as2domain_dir}")
    log.info(f"Input dir:     {input_dir}")
    log.info(f"Output dir:    {out_dir}")

    # ── 1. Load inputs ────────────────────────────────────────────────────
    log.info("Loading input files …")

    def _load(directory, fname, hint):
        p = Path(directory) / fname
        try:
            return load_json(p)
        except FileNotFoundError:
            log.error(f"Missing: {p}  ({hint})")
            raise SystemExit(1) from None

    as2domain  = _load(as2domain_dir, "as_centered_as2domain.json", "run check_domain.py first")
    as2orgname = _load(input_dir,     "as2orgname.json",             "run as_centered_as2web first")
    asn2cc     = _load(input_dir,     "asn2cc.json",                 "run as_centered_as2web first")

    # Canonical ASN universe: final_as_scope.json in the supplied input directory.
    scope_path = Path(input_dir) / "final_as_scope.json"
    try:
        scope_raw = load_json(scope_path)
    except FileNotFoundError:
        log.error(f"Missing: {scope_path}  (expected from as_centered_as2web main_pipeline)")
        raise SystemExit(1) from None
    except Exception as e:
        log.error(f"Failed to parse ASN scope file {scope_path}: {e}")
        raise SystemExit(1) from None

    scope_asns = {str(a) for a in scope_raw}

    log.info(f"as_centered_as2domain:  {len(as2domain):,} ASNs")
    log.info(f"as2orgname:             {len(as2orgname):,} ASNs")
    log.info(f"asn2cc:                 {len(asn2cc):,} ASNs")
    use_scope = not args.no_scope
    log.info(
        f"final_as_scope:         {len(scope_asns):,} ASNs "
        f"(intersection with asn2cc: {len(scope_asns & set(asn2cc.keys())):,})"
    )
    log.info(
        "Scope filter:           "
        + ("ON (targets ⊆ final_as_scope)" if use_scope else "OFF (--no_scope)")
    )

    def _in_scope(asn: str) -> bool:
        return not use_scope or asn in scope_asns

    # ── 2. Identify target ASNs ───────────────────────────────────────────
    #   category 1: in as_centered_as2domain but accessible=False
    #   category 2: in delegation (asn2cc) but not in as_centered_as2domain at all
    targets = {}   # asn -> "inaccessible" | "no_domain"
    blocked_cnt = 0 # No need to search for ASNs that the URLs exist but are blocked.
    inaccessible_cnt = 0
    no_domain_cnt = 0
    for asn, entry in as2domain.items():
        if not _in_scope(asn):
            continue
        accessible = entry.get("accessible", False)
        if not accessible:
            blocked = entry.get("reachable_blocked", False)
            if blocked:
                blocked_cnt += 1
            else:
                inaccessible_cnt += 1
                targets[asn] = "inaccessible"

    for asn in asn2cc:
        if not _in_scope(asn):
            continue
        if asn not in as2domain:
            no_domain_cnt += 1
            targets[asn] = "no_domain"

    log.info(f"Skipped (blocked):      {blocked_cnt}  "
             f"Target ASNs (total):    {len(targets)}  "
             f"(inaccessible={inaccessible_cnt}  "
             f"no_domain={no_domain_cnt})")

    # ── 2b. Reuse heuristic: skip ASNs whose identity hasn't changed ─────
    reused = {}          # asn -> {url, final_url, reused_from}
    reused_from_date = None

    if args.no_reuse:
        log.info("[reuse] Disabled (--no_reuse).")
    elif not args.whois_dir:
        log.info("[reuse] Skipped: --whois_dir not supplied "
                 "(no RIR <rir>_info.json to compare identity). Every target queried fresh.")
    else:
        combined_dir = Path(args.base_dir).parent / "combined"
        if args.prev_combined:
            prev_combined_path = Path(args.prev_combined)
        else:
            prev_date = detect_prev_date(combined_dir, date_str)
            prev_combined_path = (
                combined_dir / prev_date / "as2web.json" if prev_date else None
            )

        if prev_combined_path and prev_combined_path.exists():
            reused_from_date = prev_combined_path.parent.name
            log.info(f"[reuse] Previous snapshot: {reused_from_date}  "
                     f"({prev_combined_path})")

            prev_data = load_json(prev_combined_path)
            if isinstance(prev_data, dict) and "data" in prev_data:
                prev_data = prev_data["data"]

            # Extract previous URLs for target ASNs.
            # combine_as2web_results.py writes each ASN as {"url": ..., "sources": [...]}.
            # Older snapshots used {"<url>": [<sources>]}; fall back to the first key
            # only when there is no explicit "url" field.
            prev_urls = {}   # asn -> url from previous snapshot
            for asn in targets:
                entry = prev_data.get(asn)
                if isinstance(entry, dict) and entry:
                    url = entry.get("url") or next(iter(entry.keys()), None)
                    if url and url != "url":
                        prev_urls[asn] = url

            log.info(f"[reuse] Target ASNs with previous URL: {len(prev_urls):,}")

            if prev_urls:
                whois_base = Path(args.whois_dir)
                log.info(f"[reuse] Loading Whois identity for {reused_from_date} …")
                id_prev = load_whois_identity(whois_base, reused_from_date)
                log.info(f"[reuse] Loading Whois identity for {date_str} …")
                id_curr = load_whois_identity(whois_base, date_str)
                log.info(f"[reuse] Identity entries: prev={len(id_prev):,}  "
                         f"curr={len(id_curr):,}")

                # Filter to ASNs whose identity is unchanged
                identity_unchanged = {}
                identity_changed = 0
                identity_missing = 0
                for asn, url in prev_urls.items():
                    prev_id = id_prev.get(asn)
                    curr_id = id_curr.get(asn)
                    if prev_id is None or curr_id is None:
                        identity_missing += 1
                        continue
                    if prev_id == curr_id:
                        identity_unchanged[asn] = url
                    else:
                        identity_changed += 1

                log.info(f"[reuse] Identity unchanged: {len(identity_unchanged):,}  "
                         f"changed: {identity_changed:,}  "
                         f"missing: {identity_missing:,}")

                if identity_unchanged:
                    log.info(f"[reuse] Checking URL accessibility for "
                             f"{len(identity_unchanged):,} ASNs …")
                    url_checks = check_urls_bulk(
                        identity_unchanged, concurrency=args.workers,
                    )
                    for asn, (reachable, final_url) in url_checks.items():
                        if reachable:
                            reused[asn] = {
                                "url": final_url or identity_unchanged[asn],
                                "prev_url": identity_unchanged[asn],
                                "reused_from": reused_from_date,
                            }

                    log.info(f"[reuse] Reusable: {len(reused):,}  "
                             f"(unreachable: {len(identity_unchanged) - len(reused):,})")

                    # Remove reused ASNs from targets
                    for asn in reused:
                        targets.pop(asn, None)
                    log.info(f"[reuse] Remaining targets after reuse: {len(targets):,}")
        else:
            if args.prev_combined:
                log.warning(f"[reuse] Previous combined file not found: "
                            f"{args.prev_combined}")
            else:
                log.info("[reuse] No previous snapshot found — skipping reuse.")

    # ── 3. Build prompts (skip if no org name) ────────────────────────────
    jobs       = []   # {key, prompt, meta}
    asn_to_key = {}   # asn -> cache key (for back-fill)
    asn_meta   = {}   # asn -> {org, cc, category}
    skipped_no_org = 0
    oos_asns: set = set()
    oos_prompt_keys: set = set()
    org_prompts_new_due_to_oos: set = set()

    for asn, category in targets.items():
        org = as2orgname.get(asn, "")
        if not org:
            skipped_no_org += 1
            continue

        cc     = asn2cc.get(asn, "")
        prompt = PROMPT_TEMPLATE.format(org=org, cc_code=cc)
        key    = _sha1(prompt)

        asn_to_key[asn] = key
        asn_meta[asn]   = {"org": org, "cc": cc, "category": category}

        jobs.append({
            "key":    key,
            "prompt": prompt,
            "meta":   {"asn": asn, "org": org, "cc": cc, "category": category},
        })

    # Deduplicate jobs by key (same org+cc → one API call)
    seen_keys, unique_jobs = set(), []
    for j in jobs:
        if j["key"] not in seen_keys:
            seen_keys.add(j["key"])
            unique_jobs.append(j)

    log.info(f"ASNs with org name:     {len(asn_to_key):,}  "
             f"(skipped no-org: {skipped_no_org:,})")
    log.info(f"Unique prompts:         {len(unique_jobs):,}  "
             f"(deduped from {len(jobs):,})")

    if not use_scope:
        oos_asns = {a for a in asn_to_key if a not in scope_asns}
        oos_prompt_keys = {asn_to_key[a] for a in oos_asns}
        keys_if_scoped = {asn_to_key[a] for a in asn_to_key if a in scope_asns}
        org_prompts_new_due_to_oos = oos_prompt_keys - keys_if_scoped
        log.info(
            "Outside scope (extra):  "
            f"{len(oos_asns):,} target ASNs with org name not in final_as_scope; "
            f"{len(oos_prompt_keys):,} unique org+cc prompts touching those ASNs; "
            f"{len(org_prompts_new_due_to_oos):,} org+cc prompts only introduced by turning scope OFF"
        )

    if args.dry_run:
        log.info("[dry_run] Stopping before API calls.")
        return

    # ── 4. Load cache + find misses ───────────────────────────────────────
    cache = {} if args.no_resume else load_gz_json(cache_path)
    log.info(f"Cache loaded:           {len(cache):,} entries  ({cache_path})")

    to_query = [j for j in unique_jobs
                if j["key"] not in cache or cache[j["key"]].get("content") is None or cache[j["key"]].get("content") == ""]
    log.info(f"Cache hits:             {len(unique_jobs) - len(to_query):,}")
    log.info(f"New API calls needed:   {len(to_query):,}")

    # ── 5. API calls ──────────────────────────────────────────────────────
    if to_query:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            log.error("OPENAI_API_KEY env var not set — cannot call API.")
            raise SystemExit(1)

        if args.max_new > 0:
            to_query = to_query[:args.max_new]
            log.info(f"Capped to {args.max_new} new calls (--max_new).")

        log.info(f"Starting {len(to_query):,} API calls  "
                 f"(workers={args.workers}, model={args.model}) …")

        run_bulk(
            to_query,
            api_key=api_key,
            cache=cache,
            cache_path=cache_path,
            concurrency=args.workers,
            model=args.model,
            max_tokens=args.max_tokens,
            endpoint=args.endpoint,
            save_interval=args.save_interval,
        )

        save_gz_json_safe(cache_path, cache)
        log.info(f"Final cache saved → {cache_path}  ({len(cache):,} entries)")
    else:
        log.info("All prompts already in cache — no API calls needed.")

    # ── 6. Back-fill results per ASN ─────────────────────────────────────
    results          = {}   # full structured result per ASN
    llm_responses    = {}   # ASN        -> raw LLM text
    org_llm_responses = {}  # "org [CC]" -> raw LLM text  (deduped by prompt)
    found_cnt  = 0
    no_match   = 0
    no_content = 0

    # 6a. Insert reused results first
    for asn, info in reused.items():
        org = as2orgname.get(asn, "")
        cc  = asn2cc.get(asn, "")
        results[asn] = {
            "org":          org,
            "cc":           cc,
            "category":     "reused",
            "cache_key":    None,
            "ok":           True,
            "url":          info["url"],
            "reused":       True,
            "reused_from":  info["reused_from"],
        }
        llm_responses[asn] = f"(reused from {info['reused_from']})"
        found_cnt += 1

    # 6b. Fill in freshly-queried results
    for asn, key in asn_to_key.items():
        meta    = asn_meta[asn]
        rec     = cache.get(key) or {}
        content = rec.get("content")
        url     = parse_response(content)

        results[asn] = {
            "org":       meta["org"],
            "cc":        meta["cc"],
            "category":  meta["category"],
            "cache_key": key,
            "ok":        bool(rec.get("ok")),
            "url":       url,
        }

        text = content if content is not None else "(not yet answered)"
        llm_responses[asn] = text

        org_key = f"{meta['org']} [{meta['cc']}]"
        if org_key not in org_llm_responses:
            org_llm_responses[org_key] = text

        if url:
            found_cnt += 1
        elif content is not None:
            no_match += 1
        else:
            no_content += 1

    org_llm_path = out_dir / "org_llm_responses.json"

    dump_json_atomic(results,           out_path)
    dump_json_atomic(llm_responses,     llm_path)
    dump_json_atomic(org_llm_responses, org_llm_path)

    done_extra = ""
    if not use_scope:
        done_extra = (
            f"  Outside-scope extras : {len(oos_asns):,} ASNs, "
            f"{len(org_prompts_new_due_to_oos):,} new org+cc prompts (not needed if scope ON)\n"
        )

    log.info(
        f"\nDone.\n"
        f"  Reused from prev:     : {len(reused):,}\n"
        f"  ASNs with a found URL : {found_cnt:,}\n"
        f"  ASNs with 'No match.' : {no_match:,}\n"
        f"  ASNs not yet answered : {no_content:,}\n"
        f"{done_extra}"
        f"  Results      → {out_path}\n"
        f"  LLM (by ASN) → {llm_path}\n"
        f"  LLM (by org) → {org_llm_path}\n"
        f"  Cache        → {cache_path}"
    )


if __name__ == "__main__":
    main()
