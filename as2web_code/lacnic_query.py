import json
import subprocess
import time
import random
import os
import argparse
import datetime
import utils

def parse_date(date_str):
    try:
        return datetime.datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be in yymmdd format, e.g., 250512")

def main(asn_list, output_file):
    delay = 1  # Start with a 1-second delay
    asn_data = {}
    # Query WHOIS for each ASN with a delay
    for asn in asn_list:
        if asn in asn_data:
            print(f"Skipping {asn}, already queried.")
            continue

        while True:  # Keep retrying until successful
            try:
                print(f"Querying {asn}...")
                whois_output = subprocess.check_output(["whois", "-h", "whois.lacnic.net", asn], text=True)
                if "Query rate limit exceeded" in whois_output:
                    print(f"Rate limit exceeded for {asn}. Waiting 60 seconds before retrying...")
                    time.sleep(60)
                    continue  # Retry the same ASN after waiting
                else:
                    # Save successful result and break retry loop
                    asn_data[asn] = whois_output
                    delay = 1  # Reset delay on success
                    break  

            except subprocess.CalledProcessError as e:
                error_message = str(e)
                exit_status = e.returncode

                # Check if error is due to query rate limiting
                if "Query rate limit exceeded" in error_message or exit_status == 71:
                    print(f"Rate limit exceeded for {asn}. Waiting 60 seconds before retrying...")
                    time.sleep(60)
                    continue  # Retry the same ASN after waiting

                else:
                    print(f"Error querying {asn}: {e}")
                    asn_data[asn] = f"Error querying {asn}: {e}"
                    delay = min(delay * 2, 10)  # Exponential backoff (max 10 sec)

        # Save results after each attempt
        with open(output_file, "w") as f:
            json.dump(asn_data, f, indent=4)
        print("saved")

        time.sleep(10 + random.uniform(0, 3))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query LACNIC Whois command-line to extract Whois information.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    args = parser.parse_args()

    date = args.date

    with open(f"./data/scope/{date}/scope_rir.json", "r") as f:
        scope = json.load(f)

    # Search: LACNIC ASes in our scope (in "assigned" status on 2025-01-01 and BGP active in 2024)
    asn_list = [key for key, value in scope.items() if value == "lacnic"]
    
    output_file = f"./data/lacnic/{date}/lacnic_asn_email_data.json"
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    main(asn_list, output_file)