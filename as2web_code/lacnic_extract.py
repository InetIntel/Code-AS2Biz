import argparse
import os
from utils import find_relevant_domain, domain_filter_list
import gzip
import json
import re

def extract_lacnic_as2domain(date, input_dir, output_path):
    with open(f"{input_dir}/lacnic/{date}/lacnic_asn_email_data.json", encoding="latin-1") as f:
        data = json.load(f)

    asn_noemail = []
    need_query_as = []
    as2domain = {}
    as_org_info = {}
    for asn, whois_data in data.items():
        # Check for errors in WHOIS response
        if "Error: [Errno 61] Connection refused" in whois_data or "Query rate limit exceeded" in whois_data:
            need_query_as.append(asn)
            continue
        
        # Extract all emails of nic-hdl
        email_matches = re.findall(r"e-mail:\s+([\w\.-]+@[\w\.-]+)", whois_data)
        owner_match = re.search(r"owner:\s+(.+)", whois_data)
        owner = owner_match.group(1).strip() if owner_match else None
        if email_matches:
            domains = set(email.split("@")[1] for email in email_matches)
            domains = list(domains)
            if owner:
                domain = find_relevant_domain("", owner, domains)
                as2domain[asn] = domain
        else:
            asn_noemail.append(asn)
        as_org_info[asn] = {
            "asname": "",
            "orgid": "",
            "orgname": owner
        }

    # print("Extracted domains:", len(as2domain), "; Need query:", len(need_query_as), "; No email ASes:", len(asn_noemail))
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
    parser = argparse.ArgumentParser(description="Extract AS to domain mapping from LACNIC Whois data.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Top-level input data directory (default: ./data)")
    parser.add_argument("--output", default=None, help="Output path for cleaned JSON (default: data/Whois/lacnic/{date}/as2domain_cleaned.json)")
    args = parser.parse_args()

    output_path = args.output or f"{args.input_dir}/lacnic/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    extract_lacnic_as2domain(args.date, args.input_dir, output_path)
