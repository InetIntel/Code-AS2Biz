#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
wiki_fetch.py

Core of the optional Wikipedia classification path: turn a list of still-
unclassified ASNs into the two inputs that as2biz/prepare_openai_batch_wiki.py
consumes.

For each ASN we take its registered organisation name, strip common corporate
suffixes to a "brand", search English Wikipedia for that brand, download the
matched article's plain text, and keep it only if the brand string actually
occurs in the text (a cheap guard against wrong-topic matches).

Inputs:
  --fallback-list   JSON list of ASN strings needing classification
                    (`post_process.py sibling` writes this as fallback_as_list.json)
  --as2orgname      { "<asn>": "<registered org name>" }
                    (as2web/<date>/as2orgname.json from the AS2Web pipeline)

The opensearch guard above (brand string occurs somewhere in the article) is
deliberately cheap and lets a lot through: namesakes ("Andrew Lewis" -> a
person's biography), generic words ("Network", "Modem" -> concept articles),
acronym disambiguation pages, and wrong-topic hits ("HORIZON" -> the video
game). A second, deterministic pass resolves each kept article to its Wikidata
item and keeps it only if it is typed as an organisation -- P31 (instance of)
is not `human`/`disambiguation page`, the one-line description does not read as
a place/band/film/person/species/..., and there is a positive organisation
signal (an org-only Wikidata property such as `industry`/`legal form`/
`headquarters`/`employees`/`revenue`, or an org word in the description). A
token-similarity floor between brand and article title drops the last few
loose matches. Disable with --no-wikidata-filter.

Outputs (into --out-dir):
  wiki_info.json               { "<brand>": {"title","url","full_text"} }
                               (organisation-typed articles only, unless
                               --no-wikidata-filter)
  classifiable_as2brand.json   { "<asn>": "<brand>" }   (brands present in wiki_info)
  wiki_unmatched.json          [ "<brand>", ... ]       (no usable article)
  wiki_info.unfiltered.json    the pre-Wikidata-filter matched set (audit)
  wiki_rejected.json           { "<brand>": {"title","url","wikidata_id",
                               "wikidata_desc","reasons":[...]} }  (audit)

Wikimedia asks API clients to send a descriptive User-Agent with contact info;
pass --contact.
"""

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

import requests
import html2text


# Common corporate suffixes (case-insensitive), removed to get a search "brand".
_SUFFIXES = [
    r"llc", r"inc", r"corp", r"ltd", r"gmbh", r"s\.a\.", r"s\.p\.a\.", r"co(.,)? ltd\.?",
    r"bv", r"pjsc", r"plc", r"limited", r"ag", r"nv", r"oy", r"ab", r"sa", r"sarl",
]
_SUFFIX_RE = re.compile(r"\b(" + "|".join(_SUFFIXES) + r")\b", flags=re.IGNORECASE)


def extract_brand_name(org_name: str) -> str:
    cleaned = re.sub(r"[.,]", "", org_name)
    cleaned = _SUFFIX_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def make_session(contact: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": f"AS2Biz-WikiFetch/1.0 ({contact})",
    })
    return s


def get_wikipedia_full_text(session: requests.Session, title: str) -> str:
    endpoint = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "parse",
        "page": title,
        "format": "json",
        "prop": "text",
        "redirects": 1,
    }
    try:
        resp = session.get(endpoint, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[parse] failed for {title!r}: {e}")
        return ""

    html = resp.json().get("parse", {}).get("text", {}).get("*", "")
    if not html:
        return ""

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_tables = True
    h.body_width = 0
    return h.handle(html).strip()


def search_and_validate(session: requests.Session, brand: str) -> dict | None:
    query = brand.strip('"')
    endpoint = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "opensearch",
        "search": query,
        "limit": 1,
        "namespace": 0,
        "format": "json",
    }
    try:
        resp = session.get(endpoint, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[search] failed for {query!r}: {e}")
        return None

    data = resp.json()
    if not data[1]:
        return None

    title = data[1][0]
    page_url = data[3][0]
    full_text = get_wikipedia_full_text(session, title)
    if not full_text:
        return None
    # Cheap relevance guard: the brand string must appear in the article.
    if query.lower() not in full_text.lower():
        return None

    return {"title": title, "url": page_url, "full_text": full_text}


# ---------------------------------------------------------------------------
# Wikidata organisation-type filter
# ---------------------------------------------------------------------------

_WD_API = "https://www.wikidata.org/w/api.php"
_WD_HUMAN = "Q5"            # P31 -> instance of human
_WD_DISAMBIG = "Q4167410"   # P31 -> Wikimedia disambiguation page

# Wikidata properties that in practice only organisations carry. Presence of any
# one is a strong "this article is about an org" signal. (P17 "country" and P856
# "official website" are deliberately excluded -- places, software and games
# carry those too.)
_WD_ORG_PROPS = frozenset({
    "P452",   # industry
    "P1454",  # legal form
    "P159",   # headquarters location
    "P1128",  # employees
    "P2139",  # total revenue
    "P3362",  # operating income
    "P2403",  # total assets
    "P414",   # stock exchange
    "P169",   # chief executive officer
    "P112",   # founded by
    "P1056",  # product or material produced
})

# Positive: description reads like an organisation. Kept short on purpose --
# broad stems here ("firm", "carrier") caused false positives, so they are out.
_WD_DESC_POS = re.compile(
    r"\b(compan(y|ies)|corporat(e|ion)|incorporated|holding|conglomerate|"
    r"enterprise|bank|insurer|univers(ity|ities)|college|academy|institute|"
    r"telecommunicat\w*|telecom|operator|provider|airline|retailer|"
    r"consult\w*|agenc(y|ies)|authorit(y|ies)|cooperative|co-operative|"
    r"manufactur\w*|utility|broadcaster|publisher|distributor|ISP|"
    r"internet service|web host\w*|data cent(er|re)|registrar|foundation|"
    r"nonprofit|non-profit|association|ministr(y|ies)|"
    r"government (agenc|organi[sz]ation|body|department|enterprise)|"
    r"state-owned|GmbH|S\.A\.)\b",
    re.IGNORECASE,
)
# Negative: description reads like something that is NOT an organisation we
# would classify. An absolute veto -- rejected even with an org-only property
# (a football club has a headquarters). Deliberately excludes "software" and
# "video game": "software company" / "video game developer" ARE orgs, and a
# bare work ("Modem", "Horizon Zero Dawn") is already caught by no-org-signal.
_WD_DESC_NEG = re.compile(
    r"\b(villag\w*|towns?|cit(y|ies)|county|counties|municipalit\w*|hamlet|"
    r"commune|parish|river|creek|stream|mountain|peak|lake|island|"
    r"settlement|neighbou?rhood|districts?|provinces?|localit\w*|"
    r"bands?|albums?|singles?|songs?|films?|movies?|"
    r"(tv|television) series|card game|board game|treaty|sculpture|idol|"
    r"artwork|painting|genus|species|mollusc|dinosaur|"
    r"years?|decade|centur(y|ies)|disambiguat\w*|given name|surname|"
    r"family name|first name|politician|musician|singer|composer|painter|"
    r"author|novelist|economist|footballer|football (club|team)|"
    r"sports club|athlete|actor|actress|novels?|comics?|manga|deit(y|ies)|"
    r"mytholog\w*|supercomputer)\b",
    re.IGNORECASE,
)


def _title_tokens(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _brand_title_similarity(brand: str, title: str) -> float:
    """Jaccard over lowercase alphanumeric tokens. Cheap, order-insensitive,
    good enough to drop 'AUTOLOGO' vs 'Autologous immune enhancement therapy'
    (0.0) while keeping 'Credit bank of Moscow' vs 'Credit Bank of Moscow' (1.0)."""
    a = _title_tokens(brand.strip(" \"'"))
    b = _title_tokens(title)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _resolve_enwiki_to_qid(session: requests.Session, titles: list, sleep: float) -> dict:
    """{ requested_enwiki_title: wikidata_qid } via the enwiki API, following
    redirects and title normalisation (so 'Hyatt Corporation' -> the Q-id of
    'Hyatt', not a miss)."""
    api = "https://en.wikipedia.org/w/api.php"
    out = {}
    uniq = sorted({t for t in titles if t})
    for i in range(0, len(uniq), 40):
        chunk = uniq[i:i + 40]
        try:
            resp = session.get(api, params={
                "action": "query", "format": "json", "redirects": 1,
                "prop": "pageprops", "ppprop": "wikibase_item",
                "titles": "|".join(chunk),
            }, timeout=30)
            resp.raise_for_status()
            q = resp.json().get("query", {})
        except (requests.RequestException, ValueError) as e:
            print(f"[wikidata] enwiki resolve batch {i//40} failed: {e}")
            time.sleep(sleep)
            continue
        remap = {}
        for n in q.get("normalized", []):
            remap[n["from"]] = n["to"]
        for rd in q.get("redirects", []):
            remap[rd["from"]] = rd["to"]
        title2qid = {}
        for page in q.get("pages", {}).values():
            qid = page.get("pageprops", {}).get("wikibase_item")
            if qid:
                title2qid[page["title"]] = qid
        for t in chunk:
            cur = t
            for _ in range(4):
                if cur in title2qid:
                    out[t] = title2qid[cur]
                    break
                nxt = remap.get(cur)
                if nxt is None or nxt == cur:
                    break
                cur = nxt
        time.sleep(sleep)
    return out


def fetch_wikidata_types(session: requests.Session, titles: list, sleep: float = 0.3) -> dict:
    """{ requested_enwiki_title: {"id","desc","p31":[...],"props":frozenset()} }.
    Titles with no Wikidata item are absent from the result."""
    t2q = _resolve_enwiki_to_qid(session, titles, sleep)
    qids = sorted(set(t2q.values()))
    by_qid = {}
    for i in range(0, len(qids), 45):
        chunk = qids[i:i + 45]
        try:
            resp = session.get(_WD_API, params={
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": "claims|descriptions",
                "languages": "en",
                "format": "json",
            }, timeout=30)
            resp.raise_for_status()
            entities = resp.json().get("entities", {})
        except (requests.RequestException, ValueError) as e:
            print(f"[wikidata] wbgetentities batch {i//45} failed: {e}")
            time.sleep(sleep)
            continue
        for qid, ent in entities.items():
            if "missing" in ent:
                continue
            claims = ent.get("claims", {})
            p31 = [
                c["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
                for c in claims.get("P31", [])
                if c.get("mainsnak", {}).get("datavalue")
            ]
            by_qid[qid] = {
                "id": qid,
                "desc": ent.get("descriptions", {}).get("en", {}).get("value", ""),
                "p31": [x for x in p31 if x],
                "props": frozenset(claims) & _WD_ORG_PROPS,
            }
        time.sleep(sleep)
    return {t: by_qid[q] for t, q in t2q.items() if q in by_qid}


def wikidata_reject_reasons(wd: dict | None) -> list:
    """Empty list -> the article is typed as an organisation. Otherwise the
    list of reasons it is not."""
    if wd is None:
        return ["no-wikidata-item"]
    reasons = []
    desc = wd.get("desc", "")
    if _WD_HUMAN in wd["p31"]:
        reasons.append("wikidata-p31:human")
    if _WD_DISAMBIG in wd["p31"] or "disambiguat" in desc.lower():
        reasons.append("wikidata-p31:disambiguation")
    neg = _WD_DESC_NEG.search(desc)
    if neg:
        reasons.append(f"desc-not-org:{neg.group(0).lower()}")
    if not (wd["props"] or _WD_DESC_POS.search(desc)):
        reasons.append("no-org-signal")
    return reasons


def filter_matched_by_wikidata(session, matched: dict, min_title_sim: float,
                               sleep: float = 0.3):
    """Split `matched` into (kept, rejected). `rejected` maps brand -> audit dict."""
    wd = fetch_wikidata_types(session, [m["title"] for m in matched.values()], sleep=sleep)
    kept, rejected = {}, {}
    for brand, entry in matched.items():
        info = wd.get(entry["title"])
        reasons = wikidata_reject_reasons(info)
        sim = _brand_title_similarity(brand, entry["title"])
        if sim < min_title_sim:
            reasons.append(f"title-mismatch:{sim:.2f}")
        if reasons:
            rejected[brand] = {
                "title": entry["title"],
                "url": entry["url"],
                "wikidata_id": (info or {}).get("id"),
                "wikidata_desc": (info or {}).get("desc", ""),
                "reasons": reasons,
            }
        else:
            kept[brand] = entry
    return kept, rejected


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fallback-list", required=True,
                    help="JSON list of ASN strings needing classification.")
    ap.add_argument("--as2orgname", required=True,
                    help="JSON { \"<asn>\": \"<registered org name>\" }.")
    ap.add_argument("--out-dir", required=True, help="Directory for the output files.")
    ap.add_argument("--contact", required=True,
                    help="Contact string for the Wikimedia API User-Agent "
                         "(e.g. an email or project URL).")
    ap.add_argument("--sleep", type=float, default=0.2,
                    help="Seconds between Wikipedia requests (default 0.2).")
    ap.add_argument("--checkpoint-every", type=int, default=200,
                    help="Flush partial results every N brands.")
    ap.add_argument("--no-wikidata-filter", action="store_true",
                    help="Skip the Wikidata organisation-type pass; keep every "
                         "article whose brand string occurs in the text.")
    ap.add_argument("--wikidata-min-title-sim", type=float, default=0.34,
                    help="Drop a match whose brand/article-title token Jaccard "
                         "is below this (default 0.34).")
    args = ap.parse_args()

    fallback = load_json(Path(args.fallback_list))
    as2orgname = load_json(Path(args.as2orgname))
    out_dir = Path(args.out_dir)

    as2brand = {}
    for asn in fallback:
        asn = str(asn)
        org = as2orgname.get(asn)
        if not org:
            continue
        brand = extract_brand_name(org)
        if brand:
            as2brand[asn] = brand

    brands = sorted(set(as2brand.values()))
    print(f"{len(fallback)} fallback ASNs -> {len(as2brand)} with a brand -> "
          f"{len(brands)} unique brands to search")

    session = make_session(args.contact)
    matched = {}
    unmatched = []

    info_path = out_dir / "wiki_info.json"
    a2b_path = out_dir / "classifiable_as2brand.json"
    unm_path = out_dir / "wiki_unmatched.json"

    def flush():
        dump_json(info_path, matched)
        dump_json(a2b_path, {a: b for a, b in as2brand.items() if b in matched})
        dump_json(unm_path, unmatched)

    for i, brand in enumerate(brands, 1):
        res = search_and_validate(session, brand)
        if res:
            matched[brand] = res
        else:
            unmatched.append(brand)
        if i % args.checkpoint_every == 0:
            print(f"  {i}/{len(brands)}  matched={len(matched)}  unmatched={len(unmatched)}")
            flush()
        time.sleep(args.sleep)

    flush()
    print(f"Search done. matched brands: {len(matched)}  unmatched: {len(unmatched)}")

    if not args.no_wikidata_filter and matched:
        dump_json(out_dir / "wiki_info.unfiltered.json", matched)
        kept, rejected = filter_matched_by_wikidata(
            session, matched, args.wikidata_min_title_sim, sleep=max(args.sleep, 0.2))
        print(f"Wikidata org-type filter: {len(matched)} -> {len(kept)} kept, "
              f"{len(rejected)} rejected")
        from collections import Counter
        rc = Counter(r for v in rejected.values() for r in v["reasons"])
        for reason, n in rc.most_common():
            print(f"  reject: {reason}  x{n}")
        matched = kept
        dump_json(out_dir / "wiki_rejected.json", rejected)
        flush()

    n_asn = sum(1 for b in as2brand.values() if b in matched)
    print(f"Done. classifiable brands: {len(matched)}  ({n_asn} ASNs)")
    print(f"  {info_path}")
    print(f"  {a2b_path}")


if __name__ == "__main__":
    main()
