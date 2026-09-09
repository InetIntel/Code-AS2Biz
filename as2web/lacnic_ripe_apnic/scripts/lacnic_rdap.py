import json
import requests
import time
import random
import os
import argparse
import datetime
import utils

def parse_date(date_str):
    try:
        return datetime.datetime.strptime(date_str, "%y%m%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError("Date must be in yymmdd format, e.g., 250512") from e

def extract_emails(rdap_json):
    """Extract the list of e-mail addresses from RDAP entities."""
    emails = set()
    for ent in rdap_json.get("entities", []):
        vcard = ent.get("vcardArray", [])
        if len(vcard) == 2:
            for field in vcard[1]:
                if field[0] == "email" and len(field) > 3:
                    emails.add(field[3])
    return list(emails)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query LACNIC RDAP for per-ASN contact e-mails.")
    parser.add_argument("--date", required=True, help="Date in YYYYMMDD format (e.g., 20260301)")
    parser.add_argument("--delegation-json", required=True,
                        help="Path to the ASN registry JSON ({\"<asn>\": [\"<rir>\", \"<cc>\"]}).")
    parser.add_argument("--output-dir", required=True,
                        help="Output root; use a private, ignored directory because results contain personal data.")
    parser.add_argument("--rate-sec", type=float, default=7.0,
                        help="Base seconds to wait between RDAP requests (default 7; a small "
                             "random jitter is added). LACNIC throttles aggressively — review "
                             "its acceptable-use terms before large runs.")
    args = parser.parse_args()

    dele = utils.load_json(args.delegation_json)
    rir_as = {}
    for asn in dele:
        rir = dele[asn][0]
        rir_as.setdefault(rir, [])
        rir_as[rir].append(asn)

    for rir in rir_as:
        print(rir, len(rir_as[rir]))

    asn_list = rir_as.get("lacnic", [])
    output_dir = os.path.join(args.output_dir, args.date)
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{args.date}_lacnic_as2email.json")

    if os.path.exists(output_file):
        with open(output_file, "r") as infile:
            asn_email_map = json.load(infile)
    else:
        asn_email_map = {}

    for asn in asn_list:
        if asn in asn_email_map:
            print(f"Skipping {asn}, already queried.")
            continue

        url = f"https://rdap.lacnic.net/rdap/autnum/AS{asn}"
        while True:
            try:
                print(f"Querying {asn}...")
                r = requests.get(url, timeout=30)
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 60))
                    print(f"Rate limit hit, sleeping {retry_after} seconds...")
                    time.sleep(retry_after)
                    continue
                elif r.status_code != 200:
                    print(f"Error {r.status_code} for {asn}")
                    asn_email_map[asn] = {"error": f"HTTP {r.status_code}"}
                    break
                else:
                    data = r.json()
                    emails = extract_emails(data)
                    asn_email_map[asn] = {"emails": emails}
                    break
            except Exception as e:
                print(f"Error querying {asn}: {e}")
                time.sleep(30)
                continue

        # save
        with open(output_file, "w") as f:
            json.dump(asn_email_map, f, indent=2)
        print("saved")

        # rate limiting
        time.sleep(args.rate_sec + random.uniform(0, 2))
