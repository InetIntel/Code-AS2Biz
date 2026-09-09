#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Combine as_centered_as2web + web_search results into a single per-ASN URL view.

Logic (within final_as_scope):
  1) If only web_search has a URL → use web_search.
  2) If only as_centered_as2web has a URL → use as_centered.
  3) If both have information → prefer web_search.

Web-search URLs are re-extracted from raw LLM text with robust URL parsing
to handle responses that contain extra explanation or markdown.

Redirect sanitisation (applied to as_centered final_full_url):
  - Auth-wall redirects (Google, Microsoft, etc.) → use chosen_original,
    mark accessible=False, note="unreachable".
  - Same-org messy/encoded redirects → use chosen_original,
    keep accessible=True, note="messy_redirect_cleaned".
"""

import argparse
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known auth / identity-provider domains that indicate an unrelated redirect
# ---------------------------------------------------------------------------
KNOWN_AUTH_DOMAINS = {
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "login.yahoo.com",
    "login.salesforce.com",
    "auth.atlassian.com",
    "idp.fedoraproject.org",
    "sso.redhat.com",
    "login.okta.com",
    "auth0.com",
    "signin.aws.amazon.com",
    "console.aws.amazon.com",
    "appleid.apple.com",
    "login.twitch.tv",
    "passport.yandex.ru",
    "passport.yandex.com",
    # Cloudflare Access / Zero Trust login pages
    "cloudflareaccess.com",
}

# Patterns that indicate the final URL is a messy redirect (e.g., encoded
# portal paths, SSO bounce pages).  Applied when the domain has NOT changed.
_MESSY_QUERY_RE = re.compile(
    r"(REALMOID|SMAUTHREASON|SMAGENTNAME|TARGET=\$SM|!ut/p/|;jsessionid=)",
    re.IGNORECASE,
)


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
# Known Internet-registry / lookup domains – LLM sometimes returns these
# instead of the organisation's actual website.  Treat them as no-URL.
# ---------------------------------------------------------------------------
KNOWN_REGISTRY_DOMAINS = {
    "whois.ipip.net", "ipip.net",
    "peeringdb.com",
    "ipinfo.io",
    "bgpview.io",
    "bgp.he.net",
    "bgp.tools",
    "arin.net", "whois.arin.net",
    "ripe.net", "db.ripe.net", "apps.db.ripe.net", "stat.ripe.net",
    "apnic.net",
    "lacnic.net",
    "afrinic.net",
    "asrank.caida.org",
    "radb.net",
    "ipgeolocation.io",
    "ipapi.co",
    "team-cymru.com",
    "irrexplorer.nlnog.net",
    "radar.cloudflare.com",
    "routeviews.org",
    "he.net",
}


def is_registry_url(url: str) -> bool:
    """Return True if url points to a well-known registry/lookup site."""
    if not url:
        return False
    try:
        parsed = urlparse(url if "://" in url else "http://" + url)
        netloc = parsed.netloc.lower().split(":")[0]
        for reg_dom in KNOWN_REGISTRY_DOMAINS:
            if netloc == reg_dom or netloc.endswith("." + reg_dom):
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Case 1 – LLM response validation
# ---------------------------------------------------------------------------

# "Web search found nothing" patterns – the LLM (or the stored entry) clearly
# indicates no website exists.  Includes exact "No match.", empty strings,
# "(none)", "none", "n/a", "null", "not found", etc.
_NO_RESULT_RE = re.compile(
    r"^\s*(no\s+match\.?|none|\(none\)|n/a|null|not\s+found|–|-|—)\s*$",
    re.IGNORECASE,
)


def is_llm_no_match(text: str) -> bool:
    """Return True for responses that unambiguously mean 'no website found'."""
    if not isinstance(text, str):
        return False
    if text.strip() == "":          # empty string
        return True
    return bool(_NO_RESULT_RE.match(text))


_INVALID_LLM_PREFIXES = (
    "based on",
    "no match",
    "i couldn't",
    "i could not",
    "i was unable",
    "unfortunately",
    "i'm sorry",
    "i am sorry",
    "i don't",
    "i do not",
    "there is no",
    "there's no",
    "i cannot",
    "i can't",
    "it appears",
    "it seems",
    "after searching",
    "after extensive",
)


def is_invalid_llm_response(text: str) -> bool:
    """
    Return True when the LLM returned an explanatory sentence rather than a URL.
    Heuristic: text starts with a known "no result" phrase AND contains no URL.
    """
    if not text:
        return True
    stripped = text.strip().lower()
    has_url = bool(re.search(r"https?://|www\.", text, re.IGNORECASE))
    if has_url:
        return False
    for prefix in _INVALID_LLM_PREFIXES:
        if stripped.startswith(prefix):
            return True
    # If the whole text has no URL and looks like a sentence (contains spaces),
    # treat it as invalid when it is longer than 30 chars (clearly not a domain).
    if " " in stripped and len(stripped) > 30:
        return True
    return False


def extract_first_url(text: str):
    """
    Extract a plausible URL or domain from free-form text.
    """
    if not text:
        return None

    # First, try to match markdown formatted URL (e.g., [text](URL))
    match = re.search(r"\[.*?\]\((https?://[^\)]+)\)", text)
    if match:
        return match.group(1)

    # Second, try to match URLs that start with http(s):// or www.
    match = re.search(
        r"((?:https?://|www\.)[\w\.-]+\.[a-z]{2,}(?:[^\s\)]*))",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # Third, fallback to matching a plain domain name
    match = re.search(r"\b((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})\b", text)
    if match:
        return match.group(1)

    return None


def postprocess_url(raw_url: str):
    """
    Extracts a clean URL from a raw string that might contain trailing
    markdown references, asterisks, or punctuation.
    """
    if not raw_url:
        return None

    match = re.search(r"(https?://[^\s\]\)\*]+|www\.[^\s\]\)\*]+)", raw_url)
    if match:
        url = match.group(1)
        url = re.sub(r"[\*\)\]\.,:;!?]+$", "", url)
        return url
    return raw_url.strip()


# ---------------------------------------------------------------------------
# Cases 2 & 3 – Redirect classification
# ---------------------------------------------------------------------------

def _registered_domain(netloc: str) -> str:
    """
    Return a rough 'registered domain' (last two labels) from a netloc,
    stripping port and www. prefix.  Good enough for same-org detection.
    """
    host = netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def classify_redirect(chosen_original: str, final_full_url: str):
    """
    Decide which URL to record and whether the site is accessible.

    Returns (url_to_use: str, accessible: bool, note: str).

    Cases:
      • No final_full_url or it matches the original → keep as-is, accessible.
      • final_full_url lands on a known auth domain → use original, unreachable.
      • final_full_url domain changed to something unrelated → use original, unreachable.
      • final_full_url has messy path/query (same domain) → use original, accessible.
      • Otherwise → use final_full_url, accessible.
    """
    if not final_full_url:
        return (chosen_original, True, "")

    # Normalise for comparison: strip trailing slash
    def _norm(u):
        return u.rstrip("/").lower() if u else ""

    if _norm(final_full_url) == _norm(chosen_original):
        return (final_full_url, True, "")

    # Parse netlocs
    try:
        orig_parsed = urlparse(chosen_original if "://" in chosen_original
                               else "http://" + chosen_original)
        final_parsed = urlparse(final_full_url)
    except Exception:
        return (chosen_original, True, "")

    # Strip port from netloc for comparison (e.g. login.microsoftonline.com:443)
    final_netloc_lower = final_parsed.netloc.lower().split(":")[0]

    # Check against known auth/IdP domains first — always unreachable regardless
    # of how clean the URL looks.
    for auth_dom in KNOWN_AUTH_DOMAINS:
        if final_netloc_lower == auth_dom or final_netloc_lower.endswith("." + auth_dom):
            return (chosen_original, False, "unreachable")

    original_domain = _registered_domain(orig_parsed.netloc or orig_parsed.path)
    final_domain = _registered_domain(final_parsed.netloc or final_parsed.path)
    if original_domain and final_domain and original_domain != final_domain:
        return (chosen_original, False, "unreachable")

    # Check for messy / SSO-encoded redirects — applies regardless of whether
    # the domain changed.  Use the original clean URL but keep accessible=True.
    path = final_parsed.path or ""
    query = final_parsed.query or ""

    if _MESSY_QUERY_RE.search(final_full_url):
        return (chosen_original, True, "messy_redirect_cleaned")

    if len(path) > 60 or (query and len(query) > 40):
        return (chosen_original, True, "messy_redirect_cleaned")

    # Clean redirect (same or different domain) — trust the final URL.
    return (final_full_url, True, "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Combine as_centered_as2web and web_search results into a single per-ASN URL mapping."
    )
    ap.add_argument("--date", required=True, help="Date in YYYYMMDD.")
    ap.add_argument(
        "--as2domain_dir",
        required=True,
        help="Root directory where {date}/as_centered_as2domain.json lives.",
    )
    ap.add_argument(
        "--web_search_dir",
        required=True,
        help="Root directory where {date}/as2web_from_search.json and llm_responses.json live.",
    )
    ap.add_argument(
        "--scope_dir",
        required=True,
        help="Root directory where {date}/final_as_scope.json lives. Also used "
             "for {date}/as_centered_domain2source.json unless --as_centered_dir "
             "is given.",
    )
    ap.add_argument(
        "--as_centered_dir",
        default=None,
        help="Root directory where {date}/as_centered_domain2source.json lives "
             "(default: --scope_dir).",
    )
    ap.add_argument(
        "--out_dir",
        required=True,
        help="Root directory for combined per-date outputs.",
    )
    args = ap.parse_args()

    date_str = args.date
    as_centered_dir = args.as_centered_dir or args.scope_dir

    as2domain_dir = Path(args.as2domain_dir) / date_str
    web_search_dir = Path(args.web_search_dir) / date_str
    scope_path = Path(args.scope_dir) / date_str / "final_as_scope.json"
    out_dir = Path(args.out_dir) / date_str

    as2domain_path = as2domain_dir / "as_centered_as2domain.json"
    unchecked_path = as2domain_dir / "as_centered_as2domain_unchecked.json"
    domain2src_path = Path(as_centered_dir) / date_str / "as_centered_domain2source.json"
    ws_result_path = web_search_dir / "as2web_from_search.json"
    ws_llm_path = web_search_dir / "llm_responses.json"
    combined_path = out_dir / "as2web.json"
    detail_path = out_dir / "as2web_detail.json"

    log.info(f"Date:                {date_str}")
    log.info(f"as2domain path:      {as2domain_path}")
    log.info(f"unchecked path:      {unchecked_path}")
    log.info(f"domain→source path:  {domain2src_path}")
    log.info(f"web_search results:  {ws_result_path}")
    log.info(f"web_search LLM:      {ws_llm_path}")
    log.info(f"ASN scope path:      {scope_path}")
    log.info(f"Output dir:          {out_dir}")

    # Load inputs
    try:
        scope_raw = load_json(scope_path)
    except FileNotFoundError:
        log.error(f"Missing ASN scope file: {scope_path}")
        raise SystemExit(1) from None
    except Exception as e:
        log.error(f"Failed to parse ASN scope file {scope_path}: {e}")
        raise SystemExit(1) from None
    scope_asns = {str(a) for a in scope_raw}

    try:
        as2domain = load_json(as2domain_path)
    except FileNotFoundError:
        log.error(f"Missing as_centered_as2domain.json: {as2domain_path}")
        raise SystemExit(1) from None

    try:
        domain2source = load_json(domain2src_path)
    except FileNotFoundError:
        log.error(f"Missing as_centered_domain2source.json: {domain2src_path}")
        raise SystemExit(1) from None
    except Exception as e:
        log.error(f"Failed to parse as_centered_domain2source.json {domain2src_path}: {e}")
        raise SystemExit(1) from None

    try:
        ws_results = load_json(ws_result_path)
    except FileNotFoundError:
        log.error(f"Missing as2web_from_search.json: {ws_result_path}")
        raise SystemExit(1) from None

    try:
        ws_llm = load_json(ws_llm_path)
    except FileNotFoundError:
        log.error(f"Missing llm_responses.json: {ws_llm_path}")
        raise SystemExit(1) from None

    try:
        as2domain_unchecked = load_json(unchecked_path)
        log.info(f"Unchecked domains loaded: {len(as2domain_unchecked):,} ASNs")
    except FileNotFoundError:
        log.warning(f"Unchecked domains file not found ({unchecked_path}); imputation disabled.")
        as2domain_unchecked = {}

    log.info(f"Scope ASNs:          {len(scope_asns):,}")
    log.info(f"as2domain ASNs:      {len(as2domain):,}")
    log.info(f"web_search ASNs:     {len(ws_results):,}")

    combined = {}
    from_web_only = 0
    from_center_only = 0
    from_both = 0
    no_url = 0
    reused_from_prev = 0
    redirect_unreachable = 0
    redirect_cleaned = 0
    center_smooth = 0
    # no-URL sub-categories (counted only when chosen_url is ultimately None)
    no_url_web_no_result = 0      # web search found nothing (No match / empty / (none))
    no_url_invalid_llm = 0        # LLM returned garbled/explanatory text
    no_url_registry_filtered = 0  # LLM or ws_entry returned a registry/whois URL
    no_url_other = 0              # not queried in web_search / other

    # Collect up to 2 examples per category for the statistics report
    _MAX_EX = 2
    ex_unreachable: list = []
    ex_cleaned: list = []
    ex_smooth: list = []
    ex_web_no_result: list = []
    ex_invalid_llm: list = []
    ex_registry_filtered: list = []
    ex_no_url_other: list = []
    no_url_imputed = 0            # no_url ASes rescued by unchecked imputation
    ex_imputed: list = []

    for asn in scope_asns:
        center_entry = as2domain.get(asn)
        ws_entry = ws_results.get(asn)
        raw_llm = ws_llm.get(asn)

        # ── URL from as_centered_as2web ──────────────────────────────────────
        center_url = None
        center_accessible = True
        center_note = ""
        if center_entry:
            chosen_original = center_entry.get("chosen_original") or ""
            final_full_url = center_entry.get("final_full_url") or ""
            raw_center = final_full_url or chosen_original
            if raw_center:
                center_url, center_accessible, center_note = classify_redirect(
                    chosen_original or raw_center, final_full_url or raw_center
                )
                if center_note == "unreachable":
                    redirect_unreachable += 1
                    if len(ex_unreachable) < _MAX_EX:
                        ex_unreachable.append((asn, chosen_original, final_full_url))
                elif center_note == "messy_redirect_cleaned":
                    redirect_cleaned += 1
                    if len(ex_cleaned) < _MAX_EX:
                        ex_cleaned.append((asn, chosen_original, final_full_url))
                else:
                    # center URL resolved cleanly (no redirect issue)
                    center_smooth += 1
                    if len(ex_smooth) < _MAX_EX and center_url:
                        ex_smooth.append((asn, chosen_original, final_full_url or chosen_original, center_url))

        # ── URL from web_search ──────────────────────────────────────────────
        ws_url = None
        ws_is_reused = False
        ws_registry_filtered = False   # flag for post-decision counting
        if ws_entry:
            ws_url = ws_entry.get("url") or None
            ws_is_reused = bool(ws_entry.get("reused"))
            # Guard: ws_entry["url"] may contain raw LLM text instead of a URL.
            if ws_url and (" " in ws_url or len(ws_url) > 200):
                candidate = extract_first_url(ws_url)
                candidate = postprocess_url(candidate) if candidate else None
                if candidate and not is_registry_url(candidate):
                    ws_url = candidate
                else:
                    ws_registry_filtered = is_registry_url(candidate) if candidate else False
                    ws_url = None
            # Registry URL stored directly in the result (e.g. from a previous run)
            if ws_url and is_registry_url(ws_url):
                ws_registry_filtered = True
                ws_url = None
        if not ws_url and isinstance(raw_llm, str):
            if not is_llm_no_match(raw_llm) and not is_invalid_llm_response(raw_llm):
                raw_extracted = extract_first_url(raw_llm)
                if raw_extracted:
                    candidate = postprocess_url(raw_extracted)
                    if candidate and is_registry_url(candidate):
                        ws_registry_filtered = True   # counted below if no_url
                    else:
                        ws_url = candidate

        # ── Precedence: web_search > as_centered ────────────────────────────
        chosen_url = None
        chosen_accessible = True
        chosen_note = ""
        from_web = False
        from_center = False

        if ws_url and not center_url:
            chosen_url = ws_url
            from_web = True
            from_web_only += 1
            if ws_is_reused:
                reused_from_prev += 1
        elif center_url and not ws_url:
            chosen_url = center_url
            chosen_accessible = center_accessible
            chosen_note = center_note
            from_center = True
            from_center_only += 1
        elif ws_url and center_url:
            chosen_url = ws_url
            from_web = True
            from_center = True
            from_both += 1
            if ws_is_reused:
                reused_from_prev += 1
        else:
            no_url += 1
            # Classify WHY this AS has no URL (for the quality report)
            if ws_registry_filtered:
                no_url_registry_filtered += 1
                if len(ex_registry_filtered) < _MAX_EX:
                    snippet = ws_entry.get("url") or (raw_llm[:120] if isinstance(raw_llm, str) else "")
                    ex_registry_filtered.append((asn, snippet))
            elif is_llm_no_match(raw_llm):
                no_url_web_no_result += 1
                if len(ex_web_no_result) < _MAX_EX:
                    ex_web_no_result.append((asn, repr(raw_llm) if raw_llm is not None else "(no llm entry)"))
            elif isinstance(raw_llm, str) and is_invalid_llm_response(raw_llm):
                no_url_invalid_llm += 1
                if len(ex_invalid_llm) < _MAX_EX:
                    ex_invalid_llm.append((asn, raw_llm[:120]))
            else:
                no_url_other += 1
                if len(ex_no_url_other) < _MAX_EX:
                    ws_url_val = ws_entry.get("url") if ws_entry else None
                    ex_no_url_other.append((
                        asn,
                        f"ws_entry={'present' if ws_entry else 'absent'}, "
                        f"ws_url={ws_url_val!r}, "
                        f"llm={raw_llm!r}",
                    ))

        # ── Build source list ────────────────────────────────────────────────
        url_sources = []
        if chosen_url:
            if from_center:
                srcs = None
                asn_sources = domain2source.get(asn, {})
                if center_entry and asn_sources:
                    for key in (
                        center_entry.get("chosen_clean"),
                        center_entry.get("chosen_original"),
                        center_entry.get("final_full_url"),
                    ):
                        if key and key in asn_sources:
                            srcs = asn_sources[key]
                            break
                if isinstance(srcs, list):
                    url_sources.extend(srcs)
            if from_web:
                if "WebSearch" not in url_sources:
                    url_sources.append("WebSearch")

        # ── Impute from unchecked domains if still no URL ────────────────────
        if chosen_url is None and as2domain_unchecked:
            raw_domains = as2domain_unchecked.get(asn) or []
            for raw_d in raw_domains:
                if not raw_d:
                    continue
                impute_url = raw_d if "://" in raw_d else "http://" + raw_d
                # Look up sources: try raw form, full URL, and stripped (no scheme/www)
                asn_srcs = domain2source.get(asn, {})
                impute_sources = []
                for key in (raw_d, impute_url):
                    found = asn_srcs.get(key)
                    if isinstance(found, list):
                        impute_sources = found
                        break
                if not impute_sources:
                    try:
                        p = urlparse(impute_url)
                        stripped = (p.netloc or p.path).removeprefix("www.").rstrip("/")
                        found = asn_srcs.get(stripped)
                        if isinstance(found, list):
                            impute_sources = found
                    except Exception:
                        pass
                chosen_url = impute_url
                url_sources = impute_sources
                chosen_accessible = False
                chosen_note = "imputed_from_unchecked"
                no_url_imputed += 1
                if len(ex_imputed) < _MAX_EX:
                    ex_imputed.append((asn, impute_url, impute_sources))
                break  # use only the first domain

        # ── Emit per-ASN detail record ───────────────────────────────────────
        if chosen_url:
            record = {
                "url": chosen_url,
                "sources": url_sources,
                "accessible": chosen_accessible,
            }
            if chosen_note:
                record["note"] = chosen_note
            combined[asn] = record
        else:
            combined[asn] = {}

    # ── Statistics report ────────────────────────────────────────────────────
    web_search_found = from_web_only + from_both
    no_url_final = no_url - no_url_imputed
    total_ases = len(scope_asns)
    # sanity check (imputed ASes are still counted inside the no_url sub-totals)
    _check = (center_smooth + redirect_cleaned + redirect_unreachable
              + web_search_found
              + no_url_web_no_result + no_url_invalid_llm
              + no_url_registry_filtered + no_url_other)
    _check_ok = (_check == total_ases)

    def _fmt_redirect(examples):
        lines = []
        for asn, orig, final in examples:
            lines.append(f"    AS{asn}: original={orig!r}")
            lines.append(f"           final_url={final!r}")
        return "\n".join(lines) if lines else "    (none)"

    def _fmt_smooth(examples):
        lines = []
        for asn, orig, final_raw, used in examples:
            lines.append(f"    AS{asn}: original={orig!r}")
            if final_raw != orig and final_raw != used:
                lines.append(f"           final_url={final_raw!r}")
            lines.append(f"           used_url={used!r}")
        return "\n".join(lines) if lines else "    (none)"

    def _fmt_llm(examples):
        lines = []
        for asn, snippet in examples:
            lines.append(f"    AS{asn}: {snippet!r}{'...' if len(snippet) == 120 else ''}")
        return "\n".join(lines) if lines else "    (none)"

    log.info(
        "\n"
        "╔══════════════════════════════════════════════════════════════╗\n"
        "║        URL quality classification (all ASes)                ║\n"
        "╠══════════════════════════════════════════════════════════════╣\n"
        f"║  Center URL – resolved cleanly      : {center_smooth:>8,}            ║\n"
        f"║  Center URL – messy redirect cleaned: {redirect_cleaned:>8,}            ║\n"
        f"║  Center URL – auth-wall unreachable : {redirect_unreachable:>8,}            ║\n"
        f"║  Web search – found URL             : {web_search_found:>8,}            ║\n"
        f"║  No URL – web search found nothing  : {no_url_web_no_result:>8,}            ║\n"
        f"║  No URL – registry URL filtered     : {no_url_registry_filtered:>8,}            ║\n"
        f"║  No URL – invalid LLM response      : {no_url_invalid_llm:>8,}            ║\n"
        f"║  No URL – not queried / other       : {no_url_other:>8,}            ║\n"
        "╠══════════════════════════════════════════════════════════════╣\n"
        f"║  Total                              : {_check:>8,}  {'✓' if _check_ok else '✗ MISMATCH'}         ║\n"
        "╠══════════════════════════════════════════════════════════════╣\n"
        f"║  Of no-URL: imputed from unchecked  : {no_url_imputed:>8,}            ║\n"
        f"║  Of no-URL: truly no URL at all     : {no_url_final:>8,}            ║\n"
        "╚══════════════════════════════════════════════════════════════╝\n"
        f"\nCenter URL – auth-wall unreachable examples (up to {_MAX_EX}):\n"
        + _fmt_redirect(ex_unreachable) +
        f"\n\nCenter URL – messy redirect cleaned examples (up to {_MAX_EX}):\n"
        + _fmt_redirect(ex_cleaned) +
        f"\n\nCenter URL – resolved cleanly examples (up to {_MAX_EX}):\n"
        + _fmt_smooth(ex_smooth) +
        f"\n\nNo URL – web search found nothing examples (up to {_MAX_EX}):\n"
        + _fmt_llm(ex_web_no_result) +
        f"\n\nNo URL – registry URL filtered examples (up to {_MAX_EX}):\n"
        + _fmt_llm(ex_registry_filtered) +
        f"\n\nNo URL – invalid LLM response examples (up to {_MAX_EX}):\n"
        + _fmt_llm(ex_invalid_llm) +
        f"\n\nNo URL – not queried / other examples (up to {_MAX_EX}):\n"
        + _fmt_llm(ex_no_url_other) +
        f"\n\nImputed from unchecked examples (up to {_MAX_EX}):\n"
        + "\n".join(
            f"    AS{asn}: url={url!r}  sources={srcs}"
            for asn, url, srcs in ex_imputed
        ) if ex_imputed else "\n\nImputed from unchecked examples (up to {_MAX_EX}):\n    (none)"
    )

    # ── Build as2web.json data dict ──────────────────────────────────────────
    # Format per ASN: {"url": "...", "sources": [...]}  or  {}
    combined_legacy = {}
    for asn, rec in combined.items():
        if rec and rec.get("url"):
            combined_legacy[asn] = {
                "url": rec["url"],
                "sources": rec.get("sources", []),
            }
        else:
            combined_legacy[asn] = {}

    # ── Shared metadata ──────────────────────────────────────────────────────
    snapshot_month = f"{date_str[:4]}-{date_str[4:6]}" if len(date_str) >= 6 else date_str
    metadata = {
        "snapshot_month": snapshot_month,
        "web_search_setting": "gpt 5.2+web_search tool",
        "total_ases": len(scope_asns),
        "ases_url_from_as_centered": from_center_only + from_both,
        "ases_url_from_web_search": from_web_only + from_both,
        "ases_url_from_both": from_both,
        "ases_url_reused_from_prev": reused_from_prev,
        "ases_no_url": no_url,
        "ases_imputed_from_unchecked": no_url_imputed,
        "ases_truly_no_url": no_url_final,
    }

    out_dir.mkdir(parents=True, exist_ok=True)

    # as2web.json  – legacy format (backward-compatible)
    dump_json_atomic({"metadata": metadata, "data": combined_legacy}, combined_path)

    # as2web_detail.json  – new format with accessible/note fields
    dump_json_atomic({"metadata": metadata, "data": combined}, detail_path)

    log.info(
        "Done.\n"
        f"  From web_search only:    {from_web_only:,}\n"
        f"  From as_centered only:   {from_center_only:,}\n"
        f"  Both (web_search wins):  {from_both:,}\n"
        f"  No URL:                  {no_url:,}\n"
        "\n"
        f"  Center smooth:           {center_smooth:,}\n"
        f"  Redirect cleaned:        {redirect_cleaned:,}\n"
        f"  Redirect→unreachable:    {redirect_unreachable:,}\n"
        f"  Web search found:        {web_search_found:,}\n"
        f"  No URL – web no result:  {no_url_web_no_result:,}\n"
        f"  No URL – registry filt.: {no_url_registry_filtered:,}\n"
        f"  No URL – invalid LLM:    {no_url_invalid_llm:,}\n"
        f"  No URL – other:          {no_url_other:,}\n"
        f"  → imputed from unchecked:{no_url_imputed:,}\n"
        f"  → truly no URL at all:   {no_url_final:,}\n"
        "\n"
        f"  Legacy output  → {combined_path}\n"
        f"  Detail output  → {detail_path}"
    )


if __name__ == "__main__":
    main()
