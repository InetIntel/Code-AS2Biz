import requests
import json
import time
import utils
import argparse
import os

# Base URL for APNIC RDAP. Review APNIC's whois/RDAP acceptable-use terms and
# confirm bulk querying at your intended volume is permitted before running.
RDAP_URL = "https://rdap.apnic.net/autnum/"


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


def extract_emails(rdap_data):
    emails = set()
    if not rdap_data:
        return emails

    if "entities" in rdap_data:
        for entity in rdap_data["entities"]:
            if "vcardArray" in entity:
                vcard = entity["vcardArray"]
                if isinstance(vcard, list) and len(vcard) > 1:
                    for item in vcard[1]:
                        if item[0] == "email" and len(item) > 3:
                            emails.add(item[3])

    return emails


def load_existing_results(output_file):
    if os.path.exists(output_file):
        try:
            with open(output_file, "r") as f:
                data = json.load(f)
            print(f"Loaded existing progress from {output_file}: {len(data)} ASNs already done.")
            return {str(k): v for k, v in data.items()}
        except Exception as e:
            print(f"Failed to load existing progress from {output_file}: {e}")
            print("Starting from empty results.")
    return {}


def save_results(all_results, output_file):
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=4)


def main(asn_list, output_file, sleep_sec=2.0):
    asn_list = [str(asn) for asn in asn_list]
    all_results = load_existing_results(output_file)

    total = len(asn_list)
    already_done = sum(1 for asn in asn_list if asn in all_results)
    print(f"Total ASNs: {total}")
    print(f"Already completed: {already_done}")
    print(f"Remaining: {total - already_done}")

    newly_processed = 0

    for i, asn in enumerate(asn_list, 1):
        if asn in all_results:
            print(f"Skipping ASN {asn} ({i}/{total}) - already done")
            continue

        print(f"Querying ASN {asn} ({i}/{total})...")
        rdap_data = query_rdap(asn)
        emails = sorted(extract_emails(rdap_data))
        print(f"Found {len(emails)} contact e-mail address(es).")

        all_results[asn] = list(emails)
        newly_processed += 1

        if newly_processed % 10 == 0:
            save_results(all_results, output_file)
            print(f"Saved progress to {output_file}. Total saved: {len(all_results)}")

        time.sleep(sleep_sec)

    save_results(all_results, output_file)
    print(f"All data saved to {output_file}. Final total: {len(all_results)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query APNIC RDAP for per-ASN contact e-mails.")
    parser.add_argument("--date", required=True, help="Date in yyyymmdd format (e.g., 20260301)")
    parser.add_argument("--delegation-json", required=True,
                        help="Path to the ASN registry JSON ({\"<asn>\": [\"<rir>\", \"<cc>\"]}).")
    parser.add_argument("--output-dir", required=True,
                        help="Output root; use a private, ignored directory because results contain personal data.")
    parser.add_argument("--sleep", type=float, default=2.0,
                        help="Seconds to wait between RDAP requests (default 2). Review "
                             "APNIC's acceptable-use terms and raise this for large runs.")
    args = parser.parse_args()

    dele = utils.load_json(args.delegation_json)

    rir_as = {}
    for asn in dele:
        rir = dele[asn][0]
        rir_as.setdefault(rir, [])
        rir_as[rir].append(str(asn))

    for rir in rir_as:
        print(rir, len(rir_as[rir]))

    asn_list = rir_as.get("apnic", [])
    output_dir = os.path.join(args.output_dir, args.date)
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{args.date}_apnic_as2email.json")

    main(asn_list, output_file, sleep_sec=args.sleep)
