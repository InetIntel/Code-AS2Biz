import argparse
import os
from utils import find_relevant_domain, domain_filter_list
import gzip
import json

def extract_apnic_asinfo(date, input_dir, output_path):
    def parse_file(base_path, key="aut-num"):
        dir_path = base_path + "/apnic.db.{0}.gz".format(key)
        data = {}
        with gzip.open(dir_path, 'rt', encoding='latin-1') as file:#
            blocks = file.read().strip().split("\n\n")
            for block in blocks:
                lines = block.split("\n")
                typ = lines[0].split(":")[0]
                if typ == "aut-num" or typ == "organisation":
                    tmp = {}
                    for line in lines:
                        items = line.split(":")
                        if items[0] not in tmp: # Only get the first line of descr
                            tmp[items[0]] = line[len(items[0])+1:].lstrip().rstrip()
                    data[lines[0].split(":")[1].lstrip().rstrip()] = tmp
        return data
    
    base_path = f"{input_dir}/apnic/{date}"
    as_org_info = {}
    as_info = parse_file(base_path, "aut-num")
    org_info = parse_file(base_path, "organisation")

    for asn in as_info:
        orgname = ""
        orgid = ""
        print(asn, as_info[asn])
        if "org" in as_info[asn]:
            orgid = as_info[asn]["org"]
            if orgid in org_info:
                orgname = org_info[orgid]["org-name"]
        if "descr" in as_info[asn]:
            if not orgname:
                orgname = as_info[asn]["descr"]
        as_org_info[asn] = {
            "asname": as_info[asn].get("as-name", ""),
            "orgid": orgid,
            "orgname": orgname
        }
    with open(output_path+"/as_info.json", "w") as f:
        json.dump(as_org_info, f, indent=2)
    return as_org_info

   

def extract_apnic_as2domain(as_info, date, input_dir, output_path):
    with open(f"{input_dir}/apnic/{date}/apnic_asn_email_data.json", encoding="latin-1") as f:
        as2emails = json.load(f)
    as2domain = {}
    for asn, emails in as2emails.items():
        asn_key = "AS" + asn
        if asn_key not in as_info:
            missing_info_count += 1
            continue

        as_name = as_info[asn_key].get("asname", "")
        org_name = as_info[asn_key].get("orgname", "")

        domain_set = {email.split("@")[1] for email in emails if "@" in email}
        domain = find_relevant_domain(as_name, org_name, domain_set)
        if domain:
            as2domain[asn] = domain

    print(f"ASes with domain (before cleaning): {len(as2domain)}")

    # Remove unmeaningful domains
    unmeaningful = [asn for asn, d in as2domain.items() if d in domain_filter_list]
    for asn in unmeaningful:
        del as2domain[asn]

    print(f"ASes with domain (after cleaning): {len(as2domain)}")

    with open(output_path+"/as2domain.json", "w") as f:
        json.dump(as2domain, f, indent=2)
    print(f"Saved cleaned mapping to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract AS to domain mapping from APNIC Whois data.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Top-level input data directory (default: ./data)")
    parser.add_argument("--output", default=None, help="Output path for cleaned JSON (default: data/Whois/apnic/{date}/as2domain_cleaned.json)")
    args = parser.parse_args()

    output_path = args.output or f"{args.input_dir}/apnic/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    as_info = extract_apnic_asinfo(args.date, args.input_dir, output_path)
    extract_apnic_as2domain(as_info, args.date, args.input_dir, output_path)
