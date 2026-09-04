import os
import re
import glob
import json
import argparse
from datetime import datetime
from dateutil.relativedelta import relativedelta

import pandas as pd
import tldextract as tldextract_lib
from urllib.parse import urlparse
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Unmeaningful domain list
# ---------------------------------------------------------------------------
unmeaningful_domain = [
    "gmail.com", "yahoo.com", "me.com", "hotmail.com",
    "lacnic.net", "ripe.net", "apnic.net", "mhs.attmail.com",
]
DELEGATION_BASE = OPERATIONAL_BASE = WHOIS_BASE = None
IPINFO_BASE = PEERINGDB_BASE = OUTPUT_BASE = None

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def load_json(path):
    with open(path) as f:
        return json.load(f)

def dump_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def makedir(path):
    os.makedirs(path, exist_ok=True)

# ---------------------------------------------------------------------------
# Nearest-date finders
# ---------------------------------------------------------------------------

def find_nearest_yyyymmdd(base_dir, req_date_str, filename):
    """
    Search direct subdirectories of base_dir whose names match YYYYMMDD.
    Among those in the same year-month as req_date_str, return the path
    base_dir/{closest_date}/{filename}, or None if nothing matches.
    """
    req_date = datetime.strptime(req_date_str, "%Y%m%d")
    req_ym   = req_date_str[:6]

    exact = os.path.join(base_dir, req_date_str, filename)
    if os.path.exists(exact):
        return exact

    try:
        entries = os.listdir(base_dir)
    except FileNotFoundError:
        return None

    def pick(same_month_only):
        best, best_diff = None, float("inf")
        for entry in entries:
            if not re.match(r"^\d{8}$", entry):
                continue
            if same_month_only and entry[:6] != req_ym:
                continue
            candidate = os.path.join(base_dir, entry, filename)
            if not os.path.exists(candidate):
                continue
            diff = abs((datetime.strptime(entry, "%Y%m%d") - req_date).days)
            if diff < best_diff:
                best, best_diff = candidate, diff
        return best

    hit = pick(same_month_only=True)
    if hit is None:
        hit = pick(same_month_only=False)
        if hit is not None:
            print(f"[WARNING] no {filename} dated {req_ym} under {base_dir}; "
                  f"falling back to {hit}")
    return hit


def find_nearest_delegation(req_date_str):
    """
    Find the nearest administrative_alive.json under the supplied delegation directory
    within the same calendar month as req_date_str (YYYYMMDD).
    """
    yyyy = req_date_str[:4]
    mm   = req_date_str[4:6]
    req_date = datetime.strptime(req_date_str, "%Y%m%d")

    exact = os.path.join(DELEGATION_BASE, yyyy, mm, req_date_str[6:8], "administrative_alive.json")
    if os.path.exists(exact):
        return exact

    # nearest within the requested month, else nearest anywhere under DELEGATION_BASE
    def scan(root_glob):
        best, best_diff = None, float("inf")
        for path in glob.glob(root_glob):
            m = re.search(r"(\d{4})[/\\](\d{2})[/\\](\d{2})[/\\]administrative_alive\.json$", path)
            if not m:
                continue
            try:
                f_date = datetime.strptime("".join(m.groups()), "%Y%m%d")
            except ValueError:
                continue
            diff = abs((f_date - req_date).days)
            if diff < best_diff:
                best, best_diff = path, diff
        return best

    hit = scan(os.path.join(DELEGATION_BASE, yyyy, mm, "*", "administrative_alive.json"))
    if hit is None:
        hit = scan(os.path.join(DELEGATION_BASE, "*", "*", "*", "administrative_alive.json"))
        if hit is not None:
            print(f"[WARNING] no administrative_alive.json dated {yyyy}-{mm}; "
                  f"falling back to {hit}")
    return hit


def find_nearest_peeringdb(req_date_str):
    """
    Find the nearest /peeringdb/{YYYYMMDD}/{YYYYMMDD}_pdb_as2url.json.
    The older dashed directory form ({YYYY-MM-DD}) is also accepted. Searches
    within the same calendar month as req_date_str (YYYYMMDD), then globally.
    """
    req_date = datetime.strptime(req_date_str, "%Y%m%d")
    req_ym   = req_date_str[:6]
    req_dash = f"{req_date_str[:4]}-{req_date_str[4:6]}-{req_date_str[6:8]}"

    for dirname in (req_date_str, req_dash):
        exact = os.path.join(PEERINGDB_BASE, dirname, f"{req_date_str}_pdb_as2url.json")
        if os.path.exists(exact):
            return exact

    try:
        entries = os.listdir(PEERINGDB_BASE)
    except FileNotFoundError:
        return None

    def pick(same_month_only):
        best, best_diff = None, float("inf")
        for entry in entries:
            if re.match(r"^\d{8}$", entry):
                entry_date_str = entry
            elif re.match(r"^\d{4}-\d{2}-\d{2}$", entry):
                entry_date_str = entry.replace("-", "")
            else:
                continue
            if same_month_only and not entry_date_str.startswith(req_ym):
                continue
            candidate = os.path.join(PEERINGDB_BASE, entry, f"{entry_date_str}_pdb_as2url.json")
            if not os.path.exists(candidate):
                continue
            diff = abs((datetime.strptime(entry_date_str, "%Y%m%d") - req_date).days)
            if diff < best_diff:
                best, best_diff = candidate, diff
        return best

    hit = pick(same_month_only=True)
    if hit is None:
        hit = pick(same_month_only=False)
        if hit is not None:
            print(f"[WARNING] no *_pdb_as2url.json dated {req_ym[:4]}-{req_ym[4:]}; falling back to {hit}")
    return hit

# ---------------------------------------------------------------------------
# Domain extraction helpers  (ported from reference)
# ---------------------------------------------------------------------------

def sld_label(url_or_host: str):
    """
    Return the label immediately left of the public suffix (used as grouping key).
    Examples:
      'bakwenatelecoms.co.za' -> 'bakwenatelecoms'
      'http://netwave.com.br' -> 'netwave'
      'network.lviv.ua'       -> 'lviv'
    Returns None on failure.
    """
    if not url_or_host:
        return None

    ext      = tldextract_lib.extract(url_or_host)
    hostname = ".".join(p for p in (ext.subdomain, ext.domain, ext.suffix) if p)
    if not hostname:
        host = urlparse(url_or_host).hostname
        if not host:
            return None
        hostname = host.lower()

    parts        = hostname.lower().strip(".").split(".")
    suffix_parts = ext.suffix.split(".") if ext.suffix else []

    if len(parts) < 2:
        return None
    if not suffix_parts:
        return parts[-2] if len(parts) >= 2 else parts[0]

    structured_sld_heads = {"com", "co", "net", "org", "gov", "edu", "ac", "mil", "ne", "or"}

    if len(suffix_parts) >= 2 and len(suffix_parts[-1]) == 2:
        if suffix_parts[0] in structured_sld_heads:
            # e.g. com.br / co.za / org.uk  -> one extra level to the left
            idx = -(len(suffix_parts) + 1)
            return parts[idx] if len(parts) >= len(suffix_parts) + 1 else None
        else:
            # geographic SLD (e.g. lviv.ua)
            return parts[-2]
    else:
        return parts[-2]


def extract_domain(url_or_host: str) -> str:
    """
    Return the registrable domain (domain label + public suffix).
    Examples:
      'http://a.b.netwave.com.br/x' -> 'netwave.com.br'
      'bakwenatelecoms.co.za'       -> 'bakwenatelecoms.co.za'
    """
    ext = tldextract_lib.extract(url_or_host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    host = urlparse(url_or_host).hostname
    if not host:
        host = url_or_host.split("/")[0]
    return host.split(":")[0]


def find_relevant_website(asname: str, orgname: str, sld_list):
    """
    Score each SLD against asname and orgname using fuzzy partial ratio.
    Returns a list of all SLDs tied for the best score (handles ties).
    """
    asname  = asname.lower()
    orgname = orgname.lower()
    scores  = []
    for sld in sld_list:
        s     = sld.lower()
        score = 0.5 * fuzz.partial_ratio(s, asname) + 0.5 * fuzz.partial_ratio(s, orgname)
        scores.append((sld, score))
    if not scores:
        return []
    max_score = max(sc for _, sc in scores)
    return [sld for sld, sc in scores if sc == max_score]

# ---------------------------------------------------------------------------
# RIR info helpers
# ---------------------------------------------------------------------------

def get_asname_orgname(asn, rir, asinfo):
    """
    Look up (asname, orgname) from the RIR info dict.
    Returns (None, None) when the ASN is absent from the info file.
    """
    if rir in ("arin", "ripe", "apnic", "afrinic", "jpnic"):
        hdl = "AS" + asn
        if hdl not in asinfo:
            hdl = "as" + asn
            if hdl not in asinfo:
                return None, None
        entry  = asinfo[hdl]
        asname = entry.get("ASName" if rir == "arin" else "as-name", "")
        orgname = entry.get("org", entry.get("descr", ""))
        return asname, orgname
    elif rir == "lacnic":
        if asn not in asinfo:
            return None, None
        # lacnic_info values are plain org-name strings
        return "", asinfo[asn]
    return None, None

# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process(date_str):
    req_date = datetime.strptime(date_str, "%Y%m%d")

    # ── 1. Delegation ──────────────────────────────────────────────────────
    dele_path = find_nearest_delegation(date_str)
    if not dele_path:
        print(f"[ERROR] Delegation file not found for {date_str}")
        return
    print(f"[INFO] Delegation:          {dele_path}")
    dele = load_json(dele_path)
    print(f"[INFO] #Delegated ASes:     {len(dele)}")

    # ── 2. Operational lifetime → scope ────────────────────────────────────
    ops_path = find_nearest_yyyymmdd(OPERATIONAL_BASE, date_str, "operational_lifetimes.csv")
    if not ops_path:
        print("[WARNING] Operational lifetimes not found; treating all delegated ASes as in-scope")
        operational_as = set(dele.keys())
    else:
        print(f"[INFO] Operational lifetimes: {ops_path}")
        df = pd.read_csv(ops_path, parse_dates=["startdate", "enddate"])
        df = df.dropna(subset=["enddate"])
        latest_enddates = df.groupby("ASN")["enddate"].max()
        cutoff = pd.Timestamp(req_date - relativedelta(years=1))
        mask   = latest_enddates >= cutoff
        operational_as = set(latest_enddates.index[mask].astype(str).tolist())
        print(f"[INFO] #Operational ASes (>= {cutoff.date()}): {len(operational_as)}")

    final_as_scope = list(set(dele.keys()).intersection(operational_as))
    print(f"[INFO] #Final AS scope:     {len(final_as_scope)}")

    target_dir = os.path.join(OUTPUT_BASE, date_str)
    makedir(target_dir)
    dump_json(os.path.join(target_dir, "final_as_scope.json"), final_as_scope)

    # Export ASN → country-code mapping so downstream tools (e.g. web_search)
    # can read it from disk without requiring an in-memory handoff.
    asn2cc = {asn: dele[asn][1] for asn in dele}
    dump_json(os.path.join(target_dir, "asn2cc.json"), asn2cc)
    print(f"[INFO] asn2cc written:      {target_dir}/asn2cc.json  ({len(asn2cc)} entries)")

    # ── 3. as2domain for all RIRs ──────────────────────────────────────────
    rirs = ["arin", "ripe", "apnic", "afrinic", "lacnic", "jpnic"]
    rir_domain = {}
    for rir in rirs:
        path = find_nearest_yyyymmdd(os.path.join(WHOIS_BASE, rir), date_str, "as2domain.json")
        if path:
            rir_domain[rir] = load_json(path)
            print(f"[INFO] {rir:8s} as2domain:  {path}  ({len(rir_domain[rir])} entries)")
        else:
            rir_domain[rir] = {}
            print(f"[WARNING] {rir} as2domain not found")

    # ── 4. IPinfo as2domain ────────────────────────────────────────────────
    ipinfo_path = find_nearest_yyyymmdd(IPINFO_BASE, date_str, "as2domain.json")
    if ipinfo_path:
        ipinfo_as2web = load_json(ipinfo_path)
        print(f"[INFO] IPinfo as2domain:    {ipinfo_path}  ({len(ipinfo_as2web)} entries)")
    else:
        ipinfo_as2web = {}
        print("[WARNING] IPinfo as2domain not found")

    # ── 5. PeeringDB as2url ────────────────────────────────────────────────
    pdb_path = find_nearest_peeringdb(date_str)
    if pdb_path:
        pdb_raw   = load_json(pdb_path)
        pdb_as2web = {
            asn: entry for asn, entry in pdb_raw.items()
            if entry.get("accessible") and entry.get("final_full_url")
        }
        print(f"[INFO] PeeringDB:           {pdb_path}  ({len(pdb_as2web)} accessible entries)")
    else:
        pdb_as2web = {}
        print("[WARNING] PeeringDB as2url not found")

    # ── 6. RIR info files ─────────────────────────────────────────────────
    rir_asinfo = {}
    for rir in rirs:
        path = find_nearest_yyyymmdd(os.path.join(WHOIS_BASE, rir), date_str, f"{rir}_info.json")
        if path:
            rir_asinfo[rir] = load_json(path)
            print(f"[INFO] {rir:8s} info:       {path}  ({len(rir_asinfo[rir])} entries)")
        else:
            rir_asinfo[rir] = {}
            print(f"[WARNING] {rir} info not found")

    # ── 7. Main processing loop ────────────────────────────────────────────
    as2orgname                    = {}
    as_centered_source_as2domain  = {}
    as_domain_source_map          = {}

    for asn in dele:
        rir    = dele[asn][0]
        if rir == "apnic":
            if "AS" + asn in rir_asinfo["jpnic"]:
                asinfo = rir_asinfo["jpnic"]
            else: # fallback to apnic
                asinfo = rir_asinfo.get(rir, {})
        else:
            asinfo = rir_asinfo.get(rir, {})

        whois_domain = rir_domain.get(rir, {}).get(asn, "")
        pdb_web      = pdb_as2web[asn]["final_full_url"] if asn in pdb_as2web else ""
        ipinfo_web   = ipinfo_as2web.get(asn, "")

        if_exist = whois_domain or pdb_web or ipinfo_web

        if if_exist:
            sld2domain = {}   # sld -> set of registrable domains
            sld_list   = set()
            temp_map   = {}   # domain -> list of source labels

            for web, source in [
                (whois_domain, "Whois"),
                (pdb_web,      "PeeringDB"),
                (ipinfo_web,   "IPinfo"),
            ]:
                if not web:
                    continue
                sld = sld_label(web)
                if sld:
                    domain = extract_domain(web)
                    sld2domain.setdefault(sld, set())
                    sld2domain[sld].add(domain)
                    sld_list.add(sld)
                    temp_map.setdefault(domain, [])
                    if source not in temp_map[domain]:
                        temp_map[domain].append(source)

            sld_list.discard("")

            asname, orgname = get_asname_orgname(asn, rir, asinfo)
            if asname is None:
                # Not in RIR info — skip if we can't resolve a multi-SLD conflict
                if len(sld_list) > 1:
                    continue
            else:
                if orgname:
                    as2orgname[asn] = orgname

            if len(sld_list) == 0:
                continue
            elif len(sld_list) == 1:
                chosen_sld = list(sld_list)[0]
                domains    = sld2domain[chosen_sld]
                as_centered_source_as2domain[asn] = list(domains)
                as_domain_source_map[asn] = {d: temp_map[d] for d in domains}
            else:
                # Multiple conflicting SLDs — use fuzzy matching to pick the best
                best_slds          = find_relevant_website(asname or "", orgname or "", sld_list)
                most_relevant_domain = set()
                for sld in best_slds:
                    most_relevant_domain.update(sld2domain[sld])
                if any(d in unmeaningful_domain for d in most_relevant_domain):
                    continue
                as_centered_source_as2domain[asn] = list(most_relevant_domain)
                as_domain_source_map[asn] = {d: temp_map[d] for d in most_relevant_domain}
        else:
            # No domain found — still populate as2orgname from RIR info
            asname, orgname = get_asname_orgname(asn, rir, asinfo)
            # if rir == "jpnic":
            #     print(f"JPASN: {asn}", orgname)
            if asname is not None and orgname:
                as2orgname[asn] = orgname

    print(f"[INFO] #ASes in as_centered_source_as2domain: {len(as_centered_source_as2domain)}")
    print(f"[INFO] #ASes in as2orgname:                   {len(as2orgname)}")
    print(f"[INFO] #ASes in as_domain_source_map:         {len(as_domain_source_map)}")

    dump_json(os.path.join(target_dir, "as2orgname.json"),                       as2orgname)
    dump_json(os.path.join(target_dir, "as_centered_as2domain_unchecked.json"),  as_centered_source_as2domain)
    dump_json(os.path.join(target_dir, "as_centered_domain2source.json"),        as_domain_source_map)
    print(f"[INFO] Outputs written to {target_dir}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date in YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--delegation-dir", required=True, help="Root containing YYYY/MM/DD/administrative_alive.json.")
    parser.add_argument("--operational-lifetime-dir", required=True, help="Root containing YYYYMMDD/operational_lifetimes.csv.")
    parser.add_argument("--whois-dir", required=True, help="Root containing RIR/date AS2domain and info files.")
    parser.add_argument("--ipinfo-dir", required=True, help="Root containing YYYYMMDD/as2domain.json.")
    parser.add_argument("--peeringdb-dir", required=True, help="Root containing YYYYMMDD PeeringDB outputs.")
    parser.add_argument("--output-dir", required=True, help="Root for generated YYYYMMDD AS2Web files.")
    args = parser.parse_args()

    global DELEGATION_BASE, OPERATIONAL_BASE, WHOIS_BASE, IPINFO_BASE, PEERINGDB_BASE, OUTPUT_BASE
    DELEGATION_BASE = args.delegation_dir
    OPERATIONAL_BASE = args.operational_lifetime_dir
    WHOIS_BASE = args.whois_dir
    IPINFO_BASE = args.ipinfo_dir
    PEERINGDB_BASE = args.peeringdb_dir
    OUTPUT_BASE = args.output_dir

    date_in = args.date
    date_str = date_in.replace("-", "") if "-" in date_in else date_in

    print(f"Running as_centered_as2web pipeline for date {date_str}")
    process(date_str)


if __name__ == "__main__":
    main()
