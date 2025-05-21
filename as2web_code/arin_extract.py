# arin_extract.py

import argparse
import json
import os
from utils import find_relevant_domain, domain_filter_list

def extract_arin_as2domain(date, input_dir, output_path):
    base_path = f"{input_dir}/arin/{date}"

    # Load ASN blocks
    with open(f"{base_path}/asns.txt", "r", encoding='latin-1') as f:
        as_blocks = f.read().strip().split("\n\n")

    as_orgid, as_name, as_noorgid = {}, {}, []
    for block in as_blocks:
        items = block.split("\n")
        asn, orgid = "", ""
        for item in items:
            if "ASHandle:" in item:
                asn = item.split(":")[1].strip()[2:]
            elif "OrgID:" in item:
                orgid = item.split(":")[1].strip()
                if asn:
                    as_orgid[asn] = orgid
            elif "ASName:" in item and asn:
                as_name[asn] = item.split(":")[1].strip()

    # Load Org blocks
    with open(f"{base_path}/orgs.txt", "r", encoding='latin-1') as f:
        org_blocks = f.read().strip().split("\n\n")

    orgid_name, orgid_abuse = {}, {}
    for block in org_blocks:
        items = block.split("\n")
        orgid = ""
        for item in items:
            if "OrgID:" in item:
                orgid = item.split(":")[1].strip()
            elif "OrgName:" in item and orgid:
                orgid_name[orgid] = item.split(":")[1].strip()
            elif any(k in item for k in ["OrgAbuseHandle:", "OrgAdminHandle", "OrgTechHandle", "OrgNOCHandle"]):
                abuse = item.split(":")[1].strip()
                if orgid:
                    orgid_abuse.setdefault(orgid, []).append(abuse)

    # Load POC handles to emails
    with open(f"{base_path}/pocs.txt", "r", encoding='latin-1') as f:
        poc_blocks = f.read().strip().split("\n\n")

    hdl_emails = {}
    for block in poc_blocks:
        items = block.split("\n")
        hdl = ""
        emails = set()
        for item in items:
            if "POCHandle:" in item:
                hdl = item.split(":")[1].strip()
            elif "Mailbox:" in item and hdl:
                email = item.split(":")[1].strip().lower()
                if "@" in email:
                    domain = email.split("@")[1]
                    if domain != "example.com":
                        emails.add(domain)
        if emails:
            hdl_emails[hdl] = list(emails)

    # Match ASNs to domains
    as2domain, as_org_info, as_nodomain = {}, {}, []
    for asn in as_orgid:
        orgid = as_orgid[asn]
        hdls = orgid_abuse.get(orgid, [])
        domains = set()
        for hdl in hdls:
            domains.update(hdl_emails.get(hdl, []))
        if domains:
            domain = find_relevant_domain(as_name.get(asn, ""), orgid_name.get(orgid, ""), domains)
            as2domain[asn] = domain
        else:
            as_nodomain.append(asn)
        as_org_info[asn] = {
            "asname": as_name.get(asn, ""),
            "orgid": orgid,
            "orgname": orgid_name.get(orgid, "")
        }

    # Optional manual corrections
    change_email = {"mail.mil": "defense.gov/"}
    for asn, new_domain in change_email.items():
        if asn in as2domain and as2domain[asn] == list(change_email.keys())[0]:
            as2domain[asn] = new_domain

    print(f"ASes with domain (before cleaning): {len(as2domain)}")

    # Remove unmeaningful domains
    unmeaningful = [asn for asn, d in as2domain.items() if d in domain_filter_list]
    for asn in unmeaningful:
        del as2domain[asn]

    print(f"ASes with domain (after cleaning): {len(as2domain)}")

    with open(output_path+"/as2domain.json", "w") as f:
        json.dump(as2domain, f, indent=2)
    with open(output_path+"/as_info.json", "w") as f:
        json.dump(as_org_info, f, indent=2)
    print(f"Saved cleaned mapping to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract AS to domain mapping from ARIN Whois data.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Top-level data directory (default: ./data)")
    parser.add_argument("--output_dir", default=None, help="Path to output JSON file (default: data/arin/{date})")
    args = parser.parse_args()

    output_path = args.output_dir or f"{args.input_dir}/arin/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    extract_arin_as2domain(args.date, args.input_dir, output_path)
