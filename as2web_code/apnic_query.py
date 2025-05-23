import requests
import json
import time
import argparse
import os
from utils import domain_filter_list

# Base URL for APNIC RDAP
RDAP_URL = "https://rdap.apnic.net/autnum/"

# Function to query RDAP for an ASN
def query_rdap(asn):
    try:
        response = requests.get(f"{RDAP_URL}{asn}", timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"ASN {asn}: Failed with status code {response.status_code}")
            return None
    except Exception as e:
        print(f"ASN {asn}: Error {e}")
        return None

# Function to extract emails from RDAP response
def extract_emails(rdap_data):
    emails = set()
    if not rdap_data:
        return emails

    # Check entities for email information
    if "entities" in rdap_data:
        for entity in rdap_data["entities"]:
            if "vcardArray" in entity:
                vcard = entity["vcardArray"]
                if isinstance(vcard, list) and len(vcard) > 1:
                    for item in vcard[1]:
                        if item[0] == "email" and len(item) > 3:
                            emails.add(item[3])  # Email address is at index 3

    return emails

# Main processing
def main(asn_list, output_file):
    all_results = {}
    for i, asn in enumerate(asn_list, 1):
        print(f"Querying ASN {asn} ({i}/{len(asn_list)})...")
        rdap_data = query_rdap(asn)
        #print(rdap_data)
        emails = extract_emails(rdap_data)
        domains = set()
        for email in emails:
            if "@" in email:
                domain = email.split("@")[1]
                if domain not in domain_filter_list:
                    domains.add(domain)
        if domains:
            all_results[asn] = list(domains)

        # Save progress every 5 ASNs
        if i % 5 == 0:
            with open(output_file, "w") as f:
                json.dump(all_results, f, indent=4)
            print(f"Saved progress to {output_file}.")

        # Avoid rate limiting by pausing 1 second between requests
        time.sleep(11)

    # Save final results
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=4)
    print(f"All data saved to {output_file}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query APNIC RDAP to extract abuse contact emails.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    args = parser.parse_args()

    date = args.date

    with open(f"./data/scope/{date}/scope_rir.json", "r") as f:
        scope = json.load(f)

    # Search: APNIC ASes in our scope (in "assigned" status on 2025-01-01 and BGP active in 2024)
    asn_list = [key for key, value in scope.items() if value == "apnic"]

    # Output file to save results
    output_file = f"./data/apnic/{date}/as2domains.json"
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    main(asn_list, output_file)
