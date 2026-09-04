import os
import json
import gzip
import argparse
from rapidfuzz import fuzz

unmeaningful_domain = ["gmail.com", "yahoo.com", "me.com", "hotmail.com", "lacnic.net", "ripe.net", "apnic.net", "mhs.attmail.com"]
RAW_WHOIS_BASE = None
WHOIS_BASE = None

# Optional hand corrections, loaded from --manual-overrides. Shape:
#   {
#     "domain_by_asn": {"arin": {"<asn>": "<domain>"}, "afrinic": {...}},
#     "domain_rewrite": {"<from-domain>": "<to-domain>", ...}
#   }
# domain_by_asn forces a domain for specific ASNs of that RIR; domain_rewrite
# replaces any extracted domain that matches a key. Both are applied after the
# unmeaningful-domain filter. The repository ships no values here; supply your
# own file if you keep a correction list.
MANUAL_OVERRIDES = {"domain_by_asn": {}, "domain_rewrite": {}}

def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def apply_manual_overrides(as2domain, rir):
    rewrite = MANUAL_OVERRIDES.get("domain_rewrite", {})
    if rewrite:
        for asn, dom in list(as2domain.items()):
            if dom in rewrite:
                as2domain[asn] = rewrite[dom]
    by_asn = MANUAL_OVERRIDES.get("domain_by_asn", {}).get(rir, {})
    for asn, dom in by_asn.items():
        as2domain[str(asn)] = dom
    if by_asn:
        print(f"[{rir.upper()}] applied {len(by_asn)} domain_by_asn override(s)")

def dump_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def makedir(path):
    os.makedirs(path, exist_ok=True)

def find_nearest_file_in_month(base_dir, prefix, req_date_str, suffix):
    import glob
    import re
    from datetime import datetime
    req_date = datetime.strptime(req_date_str, "%Y-%m-%d")
    pattern = os.path.join(base_dir, f"{prefix}*{suffix}")
    files = glob.glob(pattern)
    if not files:
        return None

    def pick(same_month_only):
        best, best_diff = None, float("inf")
        for file in files:
            m = re.search(r'\d{4}-\d{2}-\d{2}', os.path.basename(file))
            if not m:
                continue
            d_str = m.group(0)
            if same_month_only and d_str[:7] != req_date_str[:7]:
                continue
            diff = abs((datetime.strptime(d_str, "%Y-%m-%d") - req_date).days)
            if diff < best_diff:
                best, best_diff = file, diff
        return best

    hit = pick(same_month_only=True)
    if hit is None:
        hit = pick(same_month_only=False)
        if hit is not None:
            print(f"[WARNING] no {prefix}*{suffix} dated {req_date_str[:7]}; "
                  f"falling back to {os.path.basename(hit)}")
    return hit


def get_raw_whois_file(rir, req_date_str, yyyy, mm, dd):
    raw_dir = os.path.join(RAW_WHOIS_BASE, rir, yyyy)

    if rir == "afrinic":
        exact = os.path.join(raw_dir, f"afrinic.bulkwhois.{yyyy}-{mm}-{dd}.db.txt.gz")
        if os.path.exists(exact): return exact
        return find_nearest_file_in_month(raw_dir, "afrinic.bulkwhois.", req_date_str, ".db.txt.gz")
    elif rir == "arin":
        asns = os.path.join(raw_dir, f"arin.bulkwhois.{yyyy}-{mm}-{dd}.asns.txt")
        if not os.path.exists(asns):
            asns = find_nearest_file_in_month(raw_dir, "arin.bulkwhois.", req_date_str, ".asns.txt")
        orgs = os.path.join(raw_dir, f"arin.bulkwhois.{yyyy}-{mm}-{dd}.orgs.txt")
        if not os.path.exists(orgs):
            orgs = find_nearest_file_in_month(raw_dir, "arin.bulkwhois.", req_date_str, ".orgs.txt")
        pocs = os.path.join(raw_dir, f"arin.bulkwhois.{yyyy}-{mm}-{dd}.pocs.txt")
        if not os.path.exists(pocs):
            pocs = find_nearest_file_in_month(raw_dir, "arin.bulkwhois.", req_date_str, ".pocs.txt")
        return (asns, orgs, pocs)

def find_relevant_email(as_name, org_name, emails):
    as_name, org_name = as_name.lower(), org_name.lower()
    emails = [email.lower() for email in emails]

    email_domains = [email.split('@')[-1] for email in emails]

    scores = []
    for email, domain in zip(emails, email_domains, strict=True):
        as_similarity = fuzz.partial_ratio(domain, as_name)
        org_similarity = fuzz.partial_ratio(domain, org_name)
        total_score = 0.5 * as_similarity + 0.5 * org_similarity
        scores.append((email, total_score))

    sorted_emails = sorted(scores, key=lambda x: x[1], reverse=True)
    return sorted_emails[0][0] if sorted_emails else None

def process_afrinic(date_str, date_dash):
    yyyy, mm, dd = date_dash.split("-")
    target_dir = os.path.join(WHOIS_BASE, "afrinic", date_str)
    makedir(target_dir)

    raw_file = get_raw_whois_file("afrinic", date_dash, yyyy, mm, dd)
    if not raw_file:
        print(f"AFRINIC data not found for {date_dash}")
        return

    target_symlink = os.path.join(target_dir, "afrinic.db.gz")
    if not os.path.exists(target_symlink):
        os.symlink(raw_file, target_symlink)

    print(f"Processing AFRINIC from {target_symlink}")

    file_path = target_symlink
    typ_idx = {}
    with gzip.open(file_path, 'rt', encoding='latin-1') as f:
        content = f.read()
        blocks = content.strip().split("\n\n")
        idx = 0
        for block in blocks:
            if not block.strip(): continue
            typ = block.split()[0].rstrip(":")
            typ_idx.setdefault(typ, [])
            typ_idx[typ].append(idx)
            idx += 1

    as_orgid, as_name = {}, {}
    as_noid = []

    for idx in typ_idx.get('aut-num', []):
        block = blocks[idx]
        items = block.split("\n")
        asn_item = items[0].split(':', 1)
        if len(asn_item) < 2: continue
        asn = asn_item[1].strip()
        if asn.upper().startswith("AS"): asn = asn[2:]

        find_id = False
        for item in items:
            if "org:" in item:
                orgid = item.split(':', 1)[1].strip()
                as_orgid[asn] = orgid
                find_id = True
            if "as-name:" in item:
                name = item.split(':', 1)[1].strip()
                as_name[asn] = name
        if not find_id:
            as_noid.append(asn)

    print(f"[AFRINIC] #AS2orgid: {len(as_orgid)} #AS name: {len(as_name)}")
    print(f"[AFRINIC] #No id ASes: {len(as_noid)}")

    orgid_emails = {}
    orgid_name = {}
    orgid_nodomain = []

    for idx in typ_idx.get('organisation', []):
        block = blocks[idx]
        items = block.split("\n")
        orgid_item = items[0].split(':', 1)
        if len(orgid_item) < 2: continue
        orgid = orgid_item[1].strip()

        find_id = False
        emails = set()
        for item in items:
            if "e-mail:" in item[:10]:
                val = item.split(':', 1)[1].strip()
                if "@" in val:
                    emails.add(val.split("@")[1])
                find_id = True
            if "org-name" in item[:10]:
                orgid_name[orgid] = item.split(':', 1)[1].strip()
        if not find_id:
            orgid_nodomain.append(orgid)
        else:
            orgid_emails[orgid] = list(emails)

    print(f"[AFRINIC] #orgid2emails: {len(orgid_emails)}")
    print(f"[AFRINIC] #No domain orgid: {len(orgid_nodomain)}")

    as2domain = {}
    as_org_info = {}
    as_nodomain = []

    for asn in as_orgid:
        orgid = as_orgid[asn]
        if orgid in orgid_emails:
            emails = orgid_emails[orgid]
            a_name = as_name.get(asn, "")
            o_name = orgid_name.get(orgid, "")
            domain = find_relevant_email(a_name, o_name, emails)
            as2domain[asn] = domain
            as_org_info[asn] = {"asname": a_name, "orgid": orgid, "org-name": o_name, "domain": domain}
        else:
            as_nodomain.append(asn)

    unmeaningful_whois_domain_as = []
    for asn in list(as2domain.keys()):
        if as2domain[asn] in unmeaningful_domain:
            unmeaningful_whois_domain_as.append(asn)

    print(f"[AFRINIC] ASes with Whois domain (before clean): {len(as2domain)}")
    for asn in unmeaningful_whois_domain_as:
        del as2domain[asn]
    print(f"[AFRINIC] ASes with Whois domain (after clean): {len(as2domain)}")

    apply_manual_overrides(as2domain, "afrinic")

    dump_json(os.path.join(target_dir, "as2domain.json"), as2domain)


def process_arin(date_str, date_dash):
    yyyy, mm, dd = date_dash.split("-")
    target_dir = os.path.join(WHOIS_BASE, "arin", date_str)
    makedir(target_dir)

    files = get_raw_whois_file("arin", date_dash, yyyy, mm, dd)
    if not files or not all(files):
        print(f"ARIN data not found for {date_dash}")
        return

    asns_raw, orgs_raw, pocs_raw = files
    asns_sym = os.path.join(target_dir, "asns.txt")
    orgs_sym = os.path.join(target_dir, "orgs.txt")
    pocs_sym = os.path.join(target_dir, "pocs.txt")

    if not os.path.exists(asns_sym): os.symlink(asns_raw, asns_sym)
    if not os.path.exists(orgs_sym): os.symlink(orgs_raw, orgs_sym)
    if not os.path.exists(pocs_sym): os.symlink(pocs_raw, pocs_sym)

    print(f"Processing ARIN from {target_dir}")

    with open(asns_sym, "r") as f:
        content = f.read()
        as_blocks = content.strip().split("\n\n")

    as_orgid, as_name = {}, {}
    as_noorgid = []

    for block in as_blocks:
        items = block.split("\n")
        find_as = False
        find_id = False
        asn = ""
        for item in items:
            if "ASHandle:" in item:
                val = item.split(':', 1)[1].strip()
                if val.upper().startswith("AS"): val = val[2:]
                asn = val
                find_as = True
            if "OrgID:" in item:
                orgid = item.split(':', 1)[1].strip()
                if asn:
                    as_orgid[asn] = orgid
                    find_id = True
            if "ASName:" in item:
                name = item.split(':', 1)[1].strip()
                if asn:
                    as_name[asn] = name
        if find_as and not find_id:
            as_noorgid.append(asn)

    print(f"[ARIN] #AS2orgid: {len(as_orgid)}")
    print(f"[ARIN] #No id ASes: {len(as_noorgid)}")

    with open(orgs_sym, "r") as f:
        content = f.read()
        blocks = content.strip().split("\n\n")

    orgid_name, orgid_abuse = {}, {}
    orgid_noabuse = []
    for block in blocks:
        items = block.split("\n")
        find_abuse = False
        find_id = False
        orgid = ""
        for item in items:
            if "OrgID:" in item:
                orgid = item.split(':', 1)[1].strip()
                find_id = True
            if ("OrgAbuseHandle:" in item or "OrgAdminHandle" in item or "OrgTechHandle" in item or "OrgNOCHandle" in item) and find_id:
                abuse = item.split(':', 1)[1].strip()
                orgid_abuse.setdefault(orgid, [])
                if abuse not in orgid_abuse[orgid]:
                    orgid_abuse[orgid].append(abuse)
                find_abuse = True
            if "OrgName:" in item and find_id:
                orgid_name[orgid] = item.split(':', 1)[1].strip()
        if find_id and not find_abuse:
            orgid_noabuse.append(orgid)

    print(f"[ARIN] #orgid2abuse: {len(orgid_abuse)}")
    print(f"[ARIN] #No abuse orgs: {len(orgid_noabuse)}")

    as_abuse = {}
    as_noabuse = []
    for asn in as_orgid:
        orgid = as_orgid[asn]
        if orgid in orgid_abuse:
            as_abuse[asn] = orgid_abuse[orgid]
        else:
            as_noabuse.append(asn)

    print(f"[ARIN] #ASes with abuse handle: {len(as_abuse)}")
    print(f"[ARIN] #ASes without abuse handle: {len(as_noabuse)}")

    with open(pocs_sym, "r") as f:
        content = f.read()
        poc_blocks = content.strip().split("\n\n")

    hdl_emails = {}
    for block in poc_blocks:
        if block == "": continue
        items = block.split("\n")
        find_hdl = False
        emails = set()
        hdl = ""
        for item in items:
            if "POCHandle:" in item:
                hdl = item.split(':', 1)[1].strip()
                find_hdl = True
            if "Mailbox:" in item and find_hdl:
                val = item.split(':', 1)[1].strip()
                if "@" in val:
                    domain = val.split("@")[1]
                    if domain != "example.com":
                        emails.add(domain)
        if emails and hdl:
            hdl_emails[hdl] = list(emails)

    as2domain = {}
    as_nodomain = []

    for asn in as_abuse:
        hdls = as_abuse[asn]
        emails = set()
        for hdl in hdls:
            if hdl in hdl_emails:
                emails.update(set(hdl_emails[hdl]))
        if emails:
            domain = find_relevant_email(as_name.get(asn, ""), orgid_name.get(as_orgid[asn], ""), emails)
            as2domain[asn] = domain
        else:
            as_nodomain.append(asn)

    print(f"[ARIN] #ASes with web: {len(as2domain)}")
    print(f"[ARIN] #ASes without web: {len(as_nodomain)}")

    unmeaningful_whois_domain_as = [
        asn for asn in as2domain if as2domain[asn] in unmeaningful_domain
    ]

    print(f"[ARIN] ASes with Whois domain (before clean): {len(as2domain)}")
    for asn in unmeaningful_whois_domain_as:
        del as2domain[asn]

    print(f"[ARIN] ASes with Whois domain (after clean): {len(as2domain)}")

    apply_manual_overrides(as2domain, "arin")

    dump_json(os.path.join(target_dir, "as2domain.json"), as2domain)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date in YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--raw-whois-dir", required=True,
                        help="Directory containing arin/<year> and afrinic/<year> bulk Whois files.")
    parser.add_argument("--output-dir", required=True,
                        help="Directory in which RIR/date output folders will be created.")
    parser.add_argument("--manual-overrides", default=None,
                        help="Optional JSON with hand corrections: "
                             '{"domain_by_asn": {"arin": {...}, "afrinic": {...}}, '
                             '"domain_rewrite": {"<from>": "<to>"}}.')
    args = parser.parse_args()

    global RAW_WHOIS_BASE, WHOIS_BASE, MANUAL_OVERRIDES
    RAW_WHOIS_BASE = args.raw_whois_dir
    WHOIS_BASE = args.output_dir
    if args.manual_overrides:
        loaded = load_json(args.manual_overrides)
        MANUAL_OVERRIDES = {
            "domain_by_asn": loaded.get("domain_by_asn", {}),
            "domain_rewrite": loaded.get("domain_rewrite", {}),
        }

    date_in = args.date
    if "-" in date_in:
        date_dash = date_in
        date_str = date_in.replace("-", "")
    else:
        date_str = date_in
        date_dash = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    print(f"Running pipeline for date {date_dash}")
    process_afrinic(date_str, date_dash)
    process_arin(date_str, date_dash)

if __name__ == "__main__":
    main()
