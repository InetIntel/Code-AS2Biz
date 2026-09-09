#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Export site texts from slice/store WARC:
- Input: archives dir + version tag + a JSON of URLs/hosts
- Output: per-site .txt files under --out-dir
- For each matched site, choose a winner variant host (same logic as before),
  and append the html2text() texts of *all* pages to a single .txt,
  with the landing page placed first.

All paths are supplied on the command line. No archive data is included in this repository.
"""

import re, json, gzip, argparse, hashlib
from pathlib import Path
from collections import defaultdict
from tqdm import tqdm
from urllib.parse import urlparse

try:
    import html2text
    H2T = html2text.HTML2Text()
    H2T.ignore_links = True
    H2T.ignore_images = True
    H2T.body_width = 0
except Exception:
    H2T = None

from warcio.archiveiterator import ArchiveIterator

# ---------- helpers ----------

def open_warc_stream(path: Path):
    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return open(path, "rb")

def html_to_text(s: str) -> str:
    # primary: html2text; fallback: strip tags
    if H2T:
        try:
            return H2T.handle(s)
        except Exception:
            pass
    # fallback: crude tag stripper
    try:
        t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
        t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
        t = re.sub(r"(?is)<[^>]+>", " ", t)
        return t
    except Exception:
        return ""

def normalize_text(t: str) -> str:
    t = t.replace("\r", "")
    # collapse trailing spaces before newline
    t = re.sub(r"[ \t]+\n", "\n", t)
    # collapse many blank lines
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def canonical_no_www(host_or_url: str) -> str:
    s = (host_or_url or "").strip().lower()
    if "://" in s:
        try:
            s = urlparse(s).netloc
        except Exception:
            pass
    h = s.split("/")[0]
    if h.startswith("www."):
        h = h[4:]
    if ":" in h:
        h = h.split(":",1)[0]
    return h.strip().strip(".")

def host_suffix_match(host: str, target_hosts: set[str]) -> str | None:
    if not host:
        return None
    cands = []
    h = host
    while True:
        if h in target_hosts:
            cands.append(h)
        i = h.find(".")
        if i <= 0:
            break
        h = h[i+1:]
    if not cands:
        return None
    return max(cands, key=len)

def is_root_like(url: str, variant_host: str) -> bool:
    try:
        p = urlparse(url)
        host = canonical_no_www(p.netloc)
        if host != variant_host:
            return False
        segs = [s for s in (p.path or "/").split("/") if s]
        if len(segs) == 0:
            return True
        if len(segs) == 1:
            one = segs[0].lower()
            if one in ("index", "index.html", "index.htm",
                       "home", "default.aspx", "default.html"):
                return True
        return False
    except Exception:
        return False

def safe_site_key(original_input: str) -> str:
    # Prefer canonical host, fall back to hashed key
    canon = canonical_no_www(original_input)
    if canon:
        return canon
    h = hashlib.sha1(
        original_input.encode("utf-8", "ignore"), usedforsecurity=False
    ).hexdigest()[:10]
    return f"site_{h}"

# ---------- index loaders ----------

def load_slice_index(slice_index: Path, include_screenshots=False):
    rows = []
    with open(slice_index, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            k = obj.get("kind")
            if k == "html" or (include_screenshots and k == "screenshot"):
                if obj.get("warc") and obj.get("record_id"):
                    rows.append(obj)
    return rows

def load_store_index(store_index_jsonl: Path):
    by_id, by_sha = {}, {}
    if not store_index_jsonl.exists():
        return by_id, by_sha
    with open(store_index_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            rid = obj.get("record_id")
            sha = obj.get("sha256")
            if rid: by_id[rid] = obj
            if sha: by_sha[sha] = obj
    return by_id, by_sha

# ---------- core ----------

def export_sites_texts(
    archives_dir: Path,
    version_tag: str,
    targets: list[str],
    out_dir: Path,
    text_min_chars: int = 0
):
    slice_root = archives_dir / "versions" / version_tag
    slice_warc_dir = slice_root / "warc"
    slice_index = slice_root / "index.jsonl"
    store_index = archives_dir / "store" / "index" / "store_index.jsonl"
    store_warc_dir = archives_dir / "store" / "warc"

    if not slice_index.exists():
        raise SystemExit(f"slice index not found: {slice_index}")
    if not store_index.exists():
        raise SystemExit(f"store index not found: {store_index}")

    by_store_id, _ = load_store_index(store_index)

    # 1) clean inputs: canonical_host -> original input (last one wins on collision)
    targets_clean = [str(x).strip() for x in targets if str(x).strip()]
    canon_to_input = {}
    canon_to_inputs = defaultdict(list)
    for t in targets_clean:
        canon_to_input[canonical_no_www(t)] = t
        canon_to_inputs[canonical_no_www(t)].append(t)
    target_canons = set(canon_to_input.keys())

    # 2) bucket the slice (keep only the last row per (target, variant_host, url))
    slice_latest = defaultdict(dict)  # (target_input, variant_host) -> {url -> row}
    with open(slice_index, "r", encoding="utf-8") as f:
        for seq, line in enumerate(f):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            k = obj.get("kind")
            if k != "html":
                continue
            if not (obj.get("warc") and obj.get("record_id")):
                continue

            site = obj.get("site","")
            site_canon = canonical_no_www(site)

            url = obj.get("url","") or ""
            try:
                url_host = canonical_no_www(urlparse(url).netloc)
            except Exception:
                url_host = ""

            matched_canon = host_suffix_match(site_canon, target_canons) or host_suffix_match(url_host, target_canons)
            if not matched_canon:
                continue

            target_input = canon_to_input[matched_canon]
            variant_host = url_host or site_canon
            if not variant_host:
                continue

            obj__ = dict(obj)
            obj__["__target_input"] = target_input
            obj__["__variant_host"] = variant_host
            obj__["__strict"] = (variant_host == matched_canon)
            obj__["__seq"] = seq  # line order (higher = newer)

            # key: keep only the last occurrence of each URL
            d = slice_latest[(target_input, variant_host)]
            prev = d.get(url)
            if (prev is None) or (obj__["__seq"] >= prev.get("__seq", -1)):
                d[url] = obj__

    # expand the compacted result back into the original bucket structure
    slice_buckets = defaultdict(list)
    for key, urlmap in slice_latest.items():
        for row in urlmap.values():
            slice_buckets[key].append(row)


    # 3) scan slice.warc -> find store_id; group by store.warc
    by_slice_warc_need = defaultdict(dict)  # warc -> {record_id -> row_meta}
    for (_target_input, _variant_host), lst in slice_buckets.items():
        for row in lst:
            by_slice_warc_need[row["warc"]][row["record_id"]] = row

    need_store = defaultdict(list)  # store_warc -> [ meta ]
    for warc_name, rid_map in tqdm(by_slice_warc_need.items(), desc="Scan slice for store refs"):
        p = slice_warc_dir / warc_name
        if not p.exists():
            continue
        stream = None
        try:
            stream = open_warc_stream(p)
            it = ArchiveIterator(stream, verify_http=False)
            for rec in it:
                if rec.rec_type not in ("response", "revisit"):
                    try: _ = rec.content_stream().read(1)
                    except Exception: pass
                    continue
                rid = rec.rec_headers.get_header("WARC-Record-ID")
                if rid not in rid_map:
                    try: _ = rec.content_stream().read(1)
                    except Exception: pass
                    continue
                row = rid_map[rid]
                try: _ = rec.content_stream().read(1)  # drain for safety
                except Exception: continue

                store_id = row.get("store_ref",{}).get("record_id") or rec.rec_headers.get_header("WARC-Refers-To")
                if not store_id:
                    continue
                entry = by_store_id.get(store_id)
                if not entry:
                    continue

                need_store[entry["warc"]].append({
                    "target_input": row["__target_input"],
                    "variant_host": row["__variant_host"],
                    "strict": row["__strict"],
                    "kind": row.get("kind"),
                    "url": row.get("url",""),
                    "mime": (row.get("mime") or entry.get("mime") or "").lower(),
                    "store_id": store_id
                })
        finally:
            try:
                if stream: stream.close()
            except Exception:
                pass

    # 4) read store.warc -> keep only text + length (bounded memory); de-dup (vhost,url)
    mem = defaultdict(lambda: {"variants": {}})  # target -> {variants: {vhost: {pages: [...]}}}
    for warc_name, items in tqdm(need_store.items(), desc="Extract store payloads"):
        path = store_warc_dir / warc_name
        if not path.exists():
            continue
        want = defaultdict(list)  # store_id -> [items]
        for it_ in items:
            want[it_["store_id"]].append(it_)

        seen_url_in_variant = defaultdict(set)  # (target,vhost) -> set(url)

        stream = None
        try:
            stream = open_warc_stream(path)
            it = ArchiveIterator(stream, verify_http=False)
            for rec in it:
                if rec.rec_type != "response":
                    try: _ = rec.content_stream().read(1)
                    except Exception: pass
                    continue
                rid = rec.rec_headers.get_header("WARC-Record-ID")
                if rid not in want:
                    try: _ = rec.content_stream().read(1)
                    except Exception: pass
                    continue

                payload = rec.content_stream().read()  # bytes
                # For each meta that points to this store record:
                for meta in want[rid]:
                    if meta.get("kind") != "html":
                        continue
                    url  = meta.get("url","")
                    tgt  = meta["target_input"]
                    vhost= meta["variant_host"]

                    # URL de-dup per variant
                    key = (tgt, vhost)
                    if url in seen_url_in_variant[key]:
                        continue
                    seen_url_in_variant[key].add(url)

                    # Decode & textify
                    try:
                        html = payload.decode("utf-8", errors="ignore")
                    except Exception:
                        html = ""
                    txt = normalize_text(html_to_text(html))
                    if len(txt) < text_min_chars:
                        continue

                    vv = mem[tgt]["variants"].setdefault(vhost, {"pages": []})
                    vv["pages"].append({
                        "url": url,
                        "text": txt,
                        "text_len": len(txt),
                        # NEW: carry store metadata for auditing
                        "store_record_id": rid,
                        "store_warc": warc_name,
                    })
        finally:
            try:
                if stream: stream.close()
            except Exception:
                pass

    # 5) pick the landing page within a variant; pick the winning variant within a target
    def pick_landing_and_stats(variant_host: str, pages: list[dict]):
        if not pages:
            return {"landing_idx": None, "landing_text_len": 0, "subpages": 0, "total_pages": 0}
        root_candidates = [(i, p["text_len"]) for i,p in enumerate(pages) if is_root_like(p["url"], variant_host)]
        if root_candidates:
            landing_idx = max(root_candidates, key=lambda x: x[1])[0]
        else:
            landing_idx = max(range(len(pages)), key=lambda i: pages[i]["text_len"])
        landing_text_len = pages[landing_idx]["text_len"]
        subpages = max(0, len(pages) - 1)
        return {
            "landing_idx": landing_idx,
            "landing_text_len": landing_text_len,
            "subpages": subpages,
            "total_pages": len(pages)
        }

    decisions = {}  # target_input -> winner_variant_host
    for target_input, blob in mem.items():
        canon_target = canonical_no_www(target_input)
        variants = blob["variants"]
        if not variants:
            continue

        stats = {}
        for vhost, v in variants.items():
            s = pick_landing_and_stats(vhost, v["pages"])
            s["strict"] = (vhost == canon_target)
            stats[vhost] = s

        strict_host = canon_target if canon_target in variants else None
        non_strict_hosts = [h for h in variants.keys() if h != strict_host]

        if strict_host is None:
            if non_strict_hosts:
                non_best = max(non_strict_hosts, key=lambda h: (stats[h]["subpages"], stats[h]["landing_text_len"]))
                decisions[target_input] = non_best
            continue

        s_stat = stats[strict_host]
        non_best = None
        if non_strict_hosts:
            non_best = max(non_strict_hosts, key=lambda h: (stats[h]["subpages"], stats[h]["landing_text_len"]))

        winner = strict_host
        if non_best:
            nb = stats[non_best]
            if s_stat["subpages"] == 0 and nb["subpages"] > 0:
                winner = non_best
            elif s_stat["subpages"] == 0 and nb["subpages"] == 0:
                if s_stat["landing_text_len"] < 200 and nb["landing_text_len"] >= 400:
                    winner = non_best
                else:
                    winner = strict_host
            else:
                winner = strict_host
        decisions[target_input] = winner

        mem[target_input]["_decision"] = {
            "strict_host": strict_host,
            "non_best": non_best,
            "winner": winner,
            "stats": stats
        }

    # 6) write text files: one .txt per target, landing page first
    out_dir.mkdir(parents=True, exist_ok=True)
    total_sites = 0
    final_url_map = {}
    final_url_map_full = {}
    for target_input, blob in mem.items():
        winner = decisions.get(target_input)
        if not winner:
            continue
        v = blob["variants"].get(winner, {})
        pages = v.get("pages", [])
        if not pages:
            continue

        # find the landing index + reorder
        s = blob.get("_decision", {}).get("stats", {}).get(winner)
        if s and s["landing_idx"] is not None:
            landing_idx = s["landing_idx"]
        else:
            # safe fallback: pick the longest text
            landing_idx = max(range(len(pages)), key=lambda i: pages[i]["text_len"])

        ordered = [pages[landing_idx]] + [p for i,p in enumerate(pages) if i != landing_idx]
        site_key = safe_site_key(target_input)
        # The landing page is ordered[0]
        landing = ordered[0]

        canon = canonical_no_www(target_input)
        for orig in canon_to_inputs.get(canon, [target_input]):
            final_url_map[orig] = {
                "warc_key": landing.get("url", ""),
                "txt_key": site_key,  # will usually be the same for all orig in this canon
            }

        # final_url_map[target_input] = {"warc_key": landing.get("url", ""), "txt_key": site_key}

        # Optional richer mapping with metadata
        final_url_map_full[target_input] = {
            "winner_variant_host": winner,
            "landing_url": landing.get("url", ""),
            "store_warc": landing.get("store_warc", ""),
            "store_record_id": landing.get("store_record_id", ""),
            "total_pages": len(ordered),
        }

        # write the file
        # site_key = safe_site_key(target_input)
        out_path = out_dir / f"{site_key}.txt"
        with open(out_path, "w", encoding="utf-8") as wf:
            # Header
            wf.write(f"# Site: {target_input}\n")
            wf.write(f"# Chosen variant host: {winner}\n")
            wf.write(f"# Pages: {len(ordered)}  (landing first)\n\n")

            # Landing first, then others
            for idx, p in enumerate(ordered, 1):
                label = "LANDING" if idx == 1 else f"PAGE {idx}"
                wf.write(f"===== [{label}] URL: {p.get('url','')} =====\n\n")
                wf.write(p["text"])
                wf.write("\n\n")

        total_sites += 1
    map_path = out_dir / "final_url_map.json"
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(final_url_map, f, ensure_ascii=False, indent=2)

    # map_full_path = out_dir / "final_url_map_full.json"
    # with open(map_full_path, "w", encoding="utf-8") as f:
    #     json.dump(final_url_map_full, f, ensure_ascii=False, indent=2)

    print(f"Wrote URL maps: {map_path}")
    print(f"Exported texts for {total_sites} site(s) to: {out_dir}")

def main():
    ap = argparse.ArgumentParser(description="Export per-site concatenated texts (landing first) from versions/<tag> + store.")
    ap.add_argument("--archives-dir", required=True)
    ap.add_argument("--version-tag", required=True)
    ap.add_argument("--urls-json", required=True, help="JSON array or {'urls': [...]} of URLs/hosts")
    ap.add_argument("--out-dir", required=True, help="Directory to save per-site .txt files")
    ap.add_argument("--text-min-chars", type=int, default=0, help="Filter pages by *plain text* char length")
    args = ap.parse_args()

    try:
        raw = Path(args.urls_json).read_text(encoding="utf-8")
        obj = json.loads(raw)
        if isinstance(obj, dict) and "urls" in obj:
            targets = obj["urls"]
        elif isinstance(obj, list):
            targets = obj
        else:
            raise ValueError("Expect array or {'urls': [...]} in JSON.")
        targets = [str(x).strip() for x in targets if str(x).strip()]
    except Exception as e:
        raise SystemExit(f"Failed to read urls json: {e}") from e

    export_sites_texts(
        archives_dir=Path(args.archives_dir).resolve(),
        version_tag=args.version_tag,
        targets=targets,
        out_dir=Path(args.out_dir).resolve(),
        text_min_chars=args.text_min_chars
    )

if __name__ == "__main__":
    main()
