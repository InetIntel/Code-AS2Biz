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

Outputs (into --out-dir):
  wiki_info.json               { "<brand>": {"title","url","full_text"} }
  classifiable_as2brand.json   { "<asn>": "<brand>" }   (brands present in wiki_info)
  wiki_unmatched.json          [ "<brand>", ... ]       (no usable article)

Wikimedia asks API clients to send a descriptive User-Agent with contact info;
pass --contact.
"""

import argparse
import json
import re
import time
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
    print(f"Done. matched brands: {len(matched)}  unmatched: {len(unmatched)}")
    print(f"  {info_path}")
    print(f"  {a2b_path}  ({sum(1 for b in as2brand.values() if b in matched)} ASNs)")


if __name__ == "__main__":
    main()
