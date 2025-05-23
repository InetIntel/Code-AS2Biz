# afrinic_extract.py

import argparse
import json
import gzip
import os
from utils import domain_filter_list

def extract_afrinic_as2domain(date, whois_path, output_path):

    with gzip.open(whois_path, 'rt', encoding='latin-1') as f:
        blocks = f.read().strip().split("\n\n")

    typ_idx = {}
    for idx, block in enumerate(blocks):
        typ = block.split()[0][:-1]
        typ_idx.setdefault(typ, []).append(idx)

    as_orgid, as_name, as_noid = {}, {}, []
    for idx in typ_idx.get("aut-num", []):
        items = blocks[idx].split("\n")
        asn = items[0].split(":")[1].strip()[2:]
        found = False
        for item in items:
            if "org:" in item:
                as_orgid[asn] = item.split(":")[1].strip()
                found = True
            elif "as-name:" in item:
                as_name[asn] = item.split(":")[1].strip()
        if not found:
            as_noid.append(asn)

    orgid_emails, orgid_name, org_cc, orgid_nodomain = {}, {}, {}, []
    for idx in typ_idx.get("organisation", []):
        items = blocks[idx].split("\n")
        orgid = items[0].split(":")[1].strip()
        emails = set()
        for item in items:
            if item.startswith("e-mail:"):
                parts = item.split(":")
                if len(parts) > 1 and "@" in parts[1]:
                    emails.add(parts[1].strip().lower())
            elif item.startswith("org-name:"):
                orgid_name[orgid] = item.split(":")[1].strip()
            elif item.startswith("country:"):
                org_cc[orgid] = item.split(":")[1].strip()
        if emails:
            orgid_emails[orgid] = list(emails)
        else:
            orgid_nodomain.append(orgid)

    as2domain, as_org_info, as_nodomain = {}, {}, []
    for asn in as_orgid:
        orgid = as_orgid[asn]
        if orgid in orgid_emails:
            emails = orgid_emails[orgid]
            domains = set()
            for email in emails:
                domain = email.split("@")[1]
                if domain not in domain_filter_list:
                    domains.add(domain)
            if domains:
                as2domain[asn] = list(domains)
        else:
            as_nodomain.append(asn)
        as_org_info[asn] = {
            "asname": as_name.get(asn, ""),
            "orgid": orgid,
            "orgname": orgid_name.get(orgid, ""),
            "country": org_cc.get(orgid, "")
        }

    # Manual corrections: these ASes do not have associated email fields in org-objects.
    as2domain.update({
        "11157": ["lancet.co.za"],
        "22354": ["udsm.ac.tz"],
        "36997": ["infocom.co.ug"],
        "37110": ["clubnet.mz"],
    })

    with open(output_path+"/as2domains.json", "w") as f:
        json.dump(as2domain, f, indent=2)
    with open(output_path+"/as_info.json", "w") as f:
        json.dump(as_org_info, f, indent=2)
    print(f"Saved cleaned mapping to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract AS to domain mapping from AFRINIC Whois data.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Top-level data directory (default: ./data)")
    parser.add_argument("--output_dir", default=None, help="Path to output JSON file (default: data/afrinic/{date})")
    args = parser.parse_args()

    whois_path = f"{args.input_dir}/afrinic/{args.date}/afrinic.db.gz"
    output_path = args.output_dir or f"{args.input_dir}/afrinic/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    extract_afrinic_as2domain(args.date, whois_path, output_path)
