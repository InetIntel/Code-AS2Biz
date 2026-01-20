import requests
import time
import json
import argparse
import datetime
import os
from utils import domain_filter_list

def get_abuse_contact(resource, sourceapp_id):
    url = "https://stat.ripe.net/data/abuse-contact-finder/data.json"
    params = {
        "resource": resource,
        "sourceapp": sourceapp_id
    }
    
    response = requests.get(url, params=params)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error: HTTP {response.status_code}")
        return None
    
def extract_emails(whois_data):
    emails = whois_data.get("data", {}).get("abuse_contacts", [])
    return emails

def parse_date(date_str):
    try:
        return datetime.datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be in yymmdd format, e.g., 250512")

def main(asn_list, output_file, sourceapp_id):
    cnt = 0
    asn_email_map = {}
    # Query all ASNs
    for asn in asn_list:
        cnt += 1
        got_email = False
        if asn in asn_email_map:
            print("Skip queried:", asn)
            continue
        print(f"Querying {asn}...")
        abuse_contact_data = get_abuse_contact(asn, sourceapp_id)  # Pass sourceapp_id to the function

        if abuse_contact_data:
            emails = extract_emails(abuse_contact_data)
            if emails:
                got_email = True
                email = emails[0]
                domain = email.split("@")[1]
                if domain not in domain_filter_list:
                    asn_email_map[asn] = [domain]
                print(f"Domain for {asn}: {domain}")
            else:
                asn_email_map[asn] = ""
        else:
            asn_email_map[asn] = ""

        time.sleep(11)  # Delay to respect API rate limits
        if got_email:
            with open(output_file, "w") as outfile:
                json.dump(asn_email_map, outfile, indent=4)
            

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query RIPE Abuse Contact API to extract abuse contact emails.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--sourceapp", required=True, help="Your identifier for querying RIPE data-api.")
    args = parser.parse_args()

    date = args.date
    id = args.sourceapp

    with open(f"./data/scope/{date}/scope_rir.json", "r") as f:
        scope = json.load(f)

    # Search: RIPE ASes in our scope (in "assigned" status on 2025-01-01 and BGP active in 2024)
    asn_list = [key for key, value in scope.items() if value == "ripe"]

    output_file = f"./data/ripe/{date}/as2domains.json"
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    main(asn_list, output_file, id)