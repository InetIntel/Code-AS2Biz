#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
post_process.py — merge the four AS2Biz classification sources into the final
per-ASN category mapping.

Preference order (a later source only adds categories an ASN does not already
have): website classification > sibling-organisation inheritance >
previous-snapshot reuse > Wikipedia > fallback web search.

The pipeline has two external steps, so this runs as three sub-commands:

  1. post_process.py sibling ...
        website classification + sibling-org inheritance
        -> <out>/sib_augmented_web_based_as2biz.json
        -> <out>/fallback_as_list.json      (ASNs still unclassified)

     >>> run:  wiki_fetch.py                (build wiki_info / classifiable_as2brand)
     >>>       prepare_openai_batch_wiki.py (-> <out>/wiki_results_by_asn.json)

  2. post_process.py wiki ...
        parse the Wikipedia batch results, then build the fallback web-search input
        -> <out>/wiki_based_as2biz.json
        -> <out>/fallback_web_search/fallback_extra_query_orgs.json
        -> <out>/fallback_web_search/orgname2asn.json

     >>> run:  fallback_openai_search.py --date <DATE> --result_root <result-root>
     >>>       (-> <out>/fallback_web_search/org_classification_parsed.json)

  3. post_process.py merge ...
        fold everything together
        -> <out>/as2biz.<DATE>.json

Inputs (see docs/01_inputs.md):
  --web-class     per-ASN website categories { "<asn>": ["<category>", ...] }.
                  Produced by prepare_openai_batch_process.py's
                  <batch-stem>_as2biz_main.json (download/materialize step).
  --scope         final_as_scope.json from as_centered_as2web (JSON list of ASNs).
  --iil-as2org    InetIntel AS-to-Organization mapping,
                  { "as2org": { "<asn>": {"OrgID": "..."} } }.
  --as2orgname    as2web/<date>/as2orgname.json
  --asn2cc        as2web/<date>/asn2cc.json
"""

import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from prompt import taxonomy_list as _TAXONOMY_WITH_SENTINEL

TAXONOMY = [c for c in _TAXONOMY_WITH_SENTINEL if c != "Cannot determine categories"]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Category extraction (robust to messy / truncated LLM output — used for the
# Wikipedia and fallback text, which is not pre-parsed like the website batch).
# ---------------------------------------------------------------------------

def _normalize_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("&", " and ").replace("/", " ").replace("–", "-").replace("—", "-")
    s = re.sub(r"[*_`#>\[\]\(\)]", " ", s)
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"[^\w\s,\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,-_")
    return s


def _tokenize(s: str):
    return [t for t in re.split(r"[\s,\-]+", _normalize_text(s)) if t]


def _similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _common_prefix_token_ratio(a, b):
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    matched = 0
    for i in range(min(len(ta), len(tb))):
        if ta[i] == tb[i]:
            matched += 1
        else:
            break
    return matched / min(len(ta), len(tb))


def _contains_prefix(a, b):
    a, b = _normalize_text(a), _normalize_text(b)
    if not a or not b:
        return 0.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if longer.startswith(shorter) or shorter in longer:
        return len(shorter) / len(longer)
    return 0.0


def _top_level(label):
    parts = label.split(" - ", 1)
    return _normalize_text(parts[0]) if parts else ""


def _extract_candidate_lines(response_text):
    if not response_text:
        return []
    text = response_text.replace("•", "\n").replace(";", "\n")
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates = []
    for line in raw_lines:
        cleaned = re.sub(r"^\s*[-*•]+\s*", "", line)
        cleaned = re.sub(r"^\s*\d+[\.\)]\s*", "", cleaned).strip(" *`_")
        if re.search(r"resulting categories", cleaned, re.I):
            continue
        if cleaned:
            candidates.append(cleaned)
    for m in re.findall(r'"([^"]+)"', response_text):
        if m.strip():
            candidates.append(m.strip())
    if response_text.strip():
        candidates.append(response_text.strip())
    out, seen = [], set()
    for x in candidates:
        key = _normalize_text(x)
        if key and key not in seen:
            seen.add(key)
            out.append(x)
    return out


def _best_taxonomy_match(candidate, taxonomy):
    norm_cand = _normalize_text(candidate)
    cand_top = _top_level(candidate)
    best_cat, best_score, dbg = None, -1.0, {}
    for cat in taxonomy:
        norm_cat = _normalize_text(cat)
        top_bonus = 0.0
        if " - " in candidate:
            top_bonus = 0.08 if cand_top == _top_level(cat) else -0.15
        seq = _similarity(norm_cand, norm_cat)
        prefix_tok = _common_prefix_token_ratio(norm_cand, norm_cat)
        contain = _contains_prefix(norm_cand, norm_cat)
        tok_c, tok_t = set(_tokenize(norm_cand)), set(_tokenize(norm_cat))
        jaccard = len(tok_c & tok_t) / max(1, len(tok_c | tok_t))
        score = 0.45 * prefix_tok + 0.25 * contain + 0.20 * seq + 0.10 * jaccard + top_bonus
        dbg[cat] = {"prefix_tok": prefix_tok, "contain": contain}
        if score > best_score:
            best_score, best_cat = score, cat
    return best_cat, best_score, dbg


def extract_valid_categories(response_text, taxonomy=TAXONOMY,
                             fuzzy_threshold=0.72, prefix_accept_threshold=0.78):
    if not response_text:
        return []
    matched, matched_set = [], set()
    norm_text = _normalize_text(response_text)
    taxonomy_norm = {c: _normalize_text(c) for c in taxonomy}

    for category in taxonomy:
        if re.search(re.escape(category), response_text, re.IGNORECASE) and category not in matched_set:
            matched.append(category)
            matched_set.add(category)

    for category, norm_cat in taxonomy_norm.items():
        if category not in matched_set and norm_cat and norm_cat in norm_text:
            matched.append(category)
            matched_set.add(category)

    for cand in _extract_candidate_lines(response_text):
        norm_cand = _normalize_text(cand)
        if not norm_cand or len(_tokenize(norm_cand)) < 2:
            continue
        exact = [c for c, nc in taxonomy_norm.items() if nc == norm_cand and c not in matched_set]
        if exact:
            for c in exact:
                matched.append(c)
                matched_set.add(c)
            continue
        best_cat, best_score, dbg = _best_taxonomy_match(cand, taxonomy)
        if best_cat is None or best_cat in matched_set:
            continue
        prefix_strength = max(dbg[best_cat]["prefix_tok"], dbg[best_cat]["contain"])
        if best_score >= fuzzy_threshold or prefix_strength >= prefix_accept_threshold:
            matched.append(best_cat)
            matched_set.add(best_cat)
    return matched


# ---------------------------------------------------------------------------
# Previous-snapshot reuse — RIR-whois identity, mirrors
# as2web/web_search/main.py's reuse heuristic so the two stages agree on what
# "identity unchanged" means.
# ---------------------------------------------------------------------------

_RIRS = ("arin", "ripe", "apnic", "afrinic", "lacnic")


def load_whois_identity(whois_base: Path, date_str: str) -> dict:
    """{asn_str: (as_name, org, descr)} from the RIR ``<rir>_info.json`` files
    under ``<whois_base>/<rir>/<date_str>/``. Per-RIR field mapping is copied
    verbatim from as2web's ``load_whois_identity``."""
    identity = {}
    for rir in _RIRS:
        info_path = Path(whois_base) / rir / date_str / f"{rir}_info.json"
        if not info_path.exists():
            print(f"[reuse]   WARNING: whois info not found: {info_path}")
            continue
        raw = load_json(info_path)
        for key, entry in raw.items():
            asn_str = str(key).lstrip("ASas")
            if not asn_str.isdigit():
                continue
            if rir == "arin":
                as_name, org, descr = entry.get("ASName", ""), entry.get("org", ""), ""
            elif rir == "lacnic":
                as_name, org, descr = "", (entry if isinstance(entry, str) else ""), ""
            else:
                as_name = entry.get("as-name", "")
                org = entry.get("org", "")
                descr = entry.get("descr", "")
            identity[asn_str] = (as_name, org, descr)
    return identity


def stage_reuse(args):
    """For ASNs still unclassified after ``sibling``, carry the previous
    snapshot's as2biz types forward when the RIR-whois identity
    ``(as-name, org, descr)`` is byte-identical to the previous snapshot.

    Full reuse — every previous type is carried. In the merged dataset a reused
    label gets the short provenance ``Reuse from <prev-label>``; the original
    per-type provenance from the previous snapshot is kept only in
    ``reuse_manifest.json`` (an audit artifact — NOT part of the published
    dataset). There is no re-verification (unlike as2web's URL re-ping); the
    exact 3-tuple identity match is the gate. Shrinks ``fallback_as_list.json``
    so the Wikipedia + fallback-web-search stages do less (paid) work; the
    pre-reuse list is kept alongside.
    """
    out = Path(args.out_dir)
    sib_as2biz = load_json(out / "sib_augmented_web_based_as2biz.json")
    fallback = [str(a) for a in load_json(out / "fallback_as_list.json")]

    prev = load_json(args.prev_as2biz)
    if isinstance(prev, dict) and "data" in prev:
        prev = prev["data"]
    prev_label = args.prev_label or Path(args.prev_as2biz).stem
    print(f"[reuse] previous as2biz:  {args.prev_as2biz}  ({len(prev):,} ASNs w/ types)")

    id_prev = load_whois_identity(args.whois_dir, args.prev_date)
    id_curr = load_whois_identity(args.whois_dir, args.curr_date)
    print(f"[reuse] whois identity:   prev({args.prev_date})={len(id_prev):,}  "
          f"curr({args.curr_date})={len(id_curr):,}")

    reuse_as2biz = {}
    reuse_manifest = {}
    n_no_prev = n_id_missing = n_id_changed = n_reused = 0
    still = []
    for asn in fallback:
        prev_types = prev.get(asn)
        if not prev_types:
            n_no_prev += 1
            still.append(asn)
            continue
        pi, ci = id_prev.get(asn), id_curr.get(asn)
        if pi is None or ci is None:
            n_id_missing += 1
            still.append(asn)
            continue
        if pi != ci:
            n_id_changed += 1
            still.append(asn)
            continue
        reuse_as2biz[asn] = {t: f"Reuse from {prev_label}" for t in prev_types}
        reuse_manifest[asn] = {
            "carried_from": prev_label,
            "whois_identity": "unchanged",
            # {type: its provenance in the previous snapshot}
            "prev_types": dict(prev_types),
        }
        n_reused += 1

    print(f"[reuse] fallback in:              {len(fallback):,}")
    print(f"[reuse]   no type last snapshot:  {n_no_prev:,}")
    print(f"[reuse]   whois identity missing: {n_id_missing:,}")
    print(f"[reuse]   whois identity changed: {n_id_changed:,}")
    print(f"[reuse]   REUSED (identity same): {n_reused:,}")
    print(f"[reuse] fallback out:             {len(still):,}")

    dump_json(out / "reuse_based_as2biz.json", reuse_as2biz)
    dump_json(out / "reuse_manifest.json", reuse_manifest)
    if not (out / "fallback_as_list.pre_reuse.json").exists():
        dump_json(out / "fallback_as_list.pre_reuse.json",
                  sorted(fallback, key=lambda x: int(x) if x.isdigit() else x))
    dump_json(out / "fallback_as_list.json",
              sorted(still, key=lambda x: int(x) if x.isdigit() else x))


# ---------------------------------------------------------------------------
# Stage 1: website classification + sibling-organisation inheritance
# ---------------------------------------------------------------------------

def stage_sibling(args):
    out = Path(args.out_dir)
    raw_web = load_json(args.web_class)
    web_as2biz = {
        asn: {t: "Direct - Website" for t in cats}
        for asn, cats in raw_web.items() if cats
    }
    print(f"web-classified ASes: {len(web_as2biz)}")

    scope = {str(a) for a in load_json(args.scope)}

    sib_as2biz = {k: dict(v) for k, v in web_as2biz.items()}

    if args.iil_as2org:
        iil = load_json(args.iil_as2org)["as2org"]
        scoped = scope & set(iil.keys())
        print(f"scope: {len(scope)} | with org mapping: {len(scoped)}")

        orgid2as = {}
        as2orgid = {}
        for asn in scoped:
            oid = iil[asn]["OrgID"]
            as2orgid[asn] = oid
            orgid2as.setdefault(oid, []).append(asn)

        purely = partial = 0
        for asn in scoped:
            have = set(web_as2biz.get(asn, {}).keys())
            inherit = {}
            for sib in orgid2as[as2orgid[asn]]:
                if sib == asn or sib not in web_as2biz:
                    continue
                for t in web_as2biz[sib]:
                    if t not in have:
                        inherit.setdefault(t, set()).add(sib)
            if not inherit:
                continue
            if asn not in sib_as2biz:
                sib_as2biz[asn] = {}
                purely += 1
            else:
                partial += 1
            for t, sibs in inherit.items():
                sib_as2biz[asn][t] = "Inherit from " + ", ".join(f"AS{a}" for a in sorted(sibs))
        print(f"purely sib-augmented: {purely} | partially: {partial}")
        universe = scoped
    else:
        print("no --iil-as2org: skipping sibling augmentation")
        universe = scope

    fallback = sorted(a for a in universe if a not in sib_as2biz)
    print(f"web/sibling-based ASes: {len(sib_as2biz)} | fallback ASes: {len(fallback)}")

    dump_json(out / "sib_augmented_web_based_as2biz.json", sib_as2biz)
    dump_json(out / "fallback_as_list.json", fallback)


# ---------------------------------------------------------------------------
# Stage 2: parse Wikipedia results, build fallback web-search input
# ---------------------------------------------------------------------------

def stage_wiki(args):
    out = Path(args.out_dir)
    sib_as2biz = load_json(out / "sib_augmented_web_based_as2biz.json")
    fallback_as_list = load_json(out / "fallback_as_list.json")

    wiki_as2biz = {}
    if args.wiki_results and Path(args.wiki_results).exists():
        wiki_results = load_json(args.wiki_results)
        for asn, rec in wiki_results.items():
            labels = set(extract_valid_categories(rec.get("classification", "")))
            if labels and asn not in sib_as2biz:
                wiki_as2biz[asn] = {t: "Direct - Wikipedia" for t in labels}
        print(f"wiki-classified ASes: {len(wiki_as2biz)}")
    else:
        print("no --wiki-results: skipping Wikipedia stage")
    dump_json(out / "wiki_based_as2biz.json", wiki_as2biz)

    as2orgname = load_json(args.as2orgname)
    asn2cc = load_json(args.asn2cc)

    still = set(fallback_as_list) - set(wiki_as2biz.keys())
    print(f"ASes for fallback web search: {len(still)}")

    org2cc, orgs, orgname2asn = {}, [], {}
    for asn in still:
        org = as2orgname.get(asn)
        if not org:
            continue
        if org not in org2cc:
            org2cc[org] = asn2cc.get(asn, "")
            orgs.append(f"{org} ({org2cc[org]})")
        orgname2asn.setdefault(f"{org} ({org2cc[org]})", []).append(asn)
    print(f"unique orgs to query: {len(orgs)}")

    fb = out / "fallback_web_search"
    dump_json(fb / "fallback_extra_query_orgs.json", orgs)
    dump_json(fb / "orgname2asn.json", orgname2asn)


# ---------------------------------------------------------------------------
# Stage 3: merge everything
# ---------------------------------------------------------------------------

def stage_merge(args):
    out = Path(args.out_dir)
    sib_as2biz = load_json(out / "sib_augmented_web_based_as2biz.json")
    reuse_path = out / "reuse_based_as2biz.json"
    reuse_as2biz = load_json(reuse_path) if reuse_path.exists() else {}
    wiki_as2biz = load_json(out / "wiki_based_as2biz.json")

    fb_parsed_path = out / "fallback_web_search" / "org_classification_parsed.json"
    fallback_as2biz = {}
    if fb_parsed_path.exists():
        org_parsed = load_json(fb_parsed_path)
        orgname2asn = load_json(out / "fallback_web_search" / "orgname2asn.json")
        for org, types in org_parsed.items():
            if not types or any("cannot" in t.lower() for t in types):
                continue
            for asn in orgname2asn.get(org, []):
                fallback_as2biz[asn] = {t: "Direct - Fallback Web Search AI" for t in types}
    print(f"sib/web: {len(sib_as2biz)} | reuse: {len(reuse_as2biz)} | "
          f"wiki: {len(wiki_as2biz)} | fallback: {len(fallback_as2biz)}")

    final = {}
    final.update(sib_as2biz)
    final.update(reuse_as2biz)
    final.update(wiki_as2biz)
    final.update(fallback_as2biz)

    unknown = {t for rec in final.values() for t in rec if t not in TAXONOMY}
    if unknown:
        print(f"WARNING: categories outside taxonomy: {sorted(unknown)}")

    # Per-source ASN counts for the metadata block (mirrors AS2Web's
    # ases_url_from_* keys). The five sources are disjoint by construction, so
    # they partition ``final``. Within sib_augmented, an ASN counts as
    # web-classified if it has at least one "Direct - Website" label; otherwise
    # every label is inherited and it counts as sibling-inheritance.
    n_web = sum(1 for rec in sib_as2biz.values()
                if any(p == "Direct - Website" for p in rec.values()))
    n_sibling = len(sib_as2biz) - n_web
    counts = {
        "total_ases": len(final),
        "ases_from_web_classification": n_web,
        "ases_from_sibling_inheritance": n_sibling,
        "ases_reused_from_prev": len(reuse_as2biz),
        "ases_from_wikipedia": len(wiki_as2biz),
        "ases_from_fallback_web_search": len(fallback_as2biz),
    }
    src_sum = sum(v for k, v in counts.items() if k != "total_ases")
    if src_sum != len(final):
        print(f"WARNING: per-source counts ({src_sum}) do not partition "
              f"final ({len(final)}) — sources may overlap")
    print("  " + " | ".join(f"{k}={v}" for k, v in counts.items()))

    metadata = {
        "snapshot_month": args.date,
        "web_classification_llm": args.web_classification_llm,
        "web_search_llm": args.web_search_llm,
    }
    if args.wiki_classification_llm:
        metadata["wiki_classification_llm"] = args.wiki_classification_llm
    metadata.update(counts)
    result = {"metadata": metadata, "data": final}
    dest = out / f"as2biz.{args.date}.json"
    dump_json(dest, result)
    print(f"final ASes: {len(final)}  ->  {dest}")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("sibling", help="website classification + sibling-org inheritance")
    p1.add_argument("--web-class", required=True)
    p1.add_argument("--scope", required=True)
    p1.add_argument("--iil-as2org", default=None)
    p1.add_argument("--out-dir", required=True)
    p1.set_defaults(func=stage_sibling)

    pr = sub.add_parser("reuse", help="reuse previous-snapshot as2biz for ASNs whose whois identity is unchanged")
    pr.add_argument("--out-dir", required=True)
    pr.add_argument("--prev-as2biz", required=True, help="previous snapshot as2biz.<DATE>.json")
    pr.add_argument("--whois-dir", required=True,
                    help="whois snapshot base: <whois-dir>/<rir>/<YYYYMMDD>/<rir>_info.json")
    pr.add_argument("--prev-date", required=True, help="whois date for the previous snapshot, e.g. 20260301")
    pr.add_argument("--curr-date", required=True, help="whois date for the current snapshot, e.g. 20260826")
    pr.add_argument("--prev-label", default=None, help="label for provenance strings (default: prev-as2biz stem)")
    pr.set_defaults(func=stage_reuse)

    p2 = sub.add_parser("wiki", help="parse Wikipedia results, build fallback web-search input")
    p2.add_argument("--out-dir", required=True)
    p2.add_argument("--wiki-results", default=None,
                    help="wiki_results_by_asn.json from prepare_openai_batch_wiki.py")
    p2.add_argument("--as2orgname", required=True)
    p2.add_argument("--asn2cc", required=True)
    p2.set_defaults(func=stage_wiki)

    p3 = sub.add_parser("merge", help="merge all sources into as2biz.<DATE>.json")
    p3.add_argument("--out-dir", required=True)
    p3.add_argument("--date", required=True, help="snapshot label, e.g. 2026-03")
    p3.add_argument("--web-classification-llm", default="gpt-5.2",
                    help="model for scraped-website content classification")
    p3.add_argument("--web-search-llm", default="gpt-5.2+web_search tool",
                    help="model for the fallback web-search classification")
    p3.add_argument("--wiki-classification-llm", default="gpt-5.2",
                    help="model for the Wikipedia fallback classification; "
                         "pass empty to omit the metadata key")
    p3.set_defaults(func=stage_merge)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
