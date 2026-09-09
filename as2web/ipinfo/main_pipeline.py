import os
import re
import glob
import gzip
import json
import csv
import argparse
from datetime import datetime
from collections import Counter

unmeaningful_domain = ["gmail.com", "yahoo.com", "me.com", "hotmail.com", "lacnic.net", "ripe.net", "apnic.net", "mhs.attmail.com"]
RAW_IPINFO_BASE = None
OUTPUT_BASE = None


def dump_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def makedir(path):
    os.makedirs(path, exist_ok=True)


def find_nearest_file(req_date_str):
    """
    Find the ipinfo ASN CSV closest in date to req_date_str (YYYY-MM-DD)
    within the same calendar month.
    Files are named: YYYY-MM-DD.asn.csv.gz
    """
    req_date = datetime.strptime(req_date_str, "%Y-%m-%d")

    # exact-day file wins
    exact = os.path.join(RAW_IPINFO_BASE, f"{req_date_str}.asn.csv.gz")
    if os.path.exists(exact):
        return exact

    files = glob.glob(os.path.join(RAW_IPINFO_BASE, "*.asn.csv.gz"))
    if not files:
        return None

    def pick(same_month_only):
        best, best_diff = None, float("inf")
        for file in files:
            m = re.match(r"(\d{4}-\d{2}-\d{2})\.asn\.csv\.gz$", os.path.basename(file))
            if not m:
                continue
            d_str = m.group(1)
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
            print(f"[WARNING] no IPinfo .asn.csv.gz dated {req_date_str[:7]}; "
                  f"falling back to {os.path.basename(hit)}")
    return hit


def build_asn_domain_map(csv_gz_path):
    """
    Read the gzipped CSV and return a dict mapping ASN (e.g. "AS13335") to
    its most frequently listed domain string, or None when absent.

    CSV columns: start_ip, end_ip, asn, name, domain
    Each ASN appears once per IP range it covers, so we take the mode domain.
    """
    domain_counts = {}   # asn -> Counter({domain: count})

    with gzip.open(csv_gz_path, "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            asn = row.get("asn", "").strip()
            domain = row.get("domain", "").strip()
            if not asn:
                continue
            if asn not in domain_counts:
                domain_counts[asn] = Counter()
            if domain:
                domain_counts[asn][domain] += 1

    # Build temp dict: ASN -> most common domain (str) or None
    temp = {}
    for asn, counter in domain_counts.items():
        if counter:
            temp[asn] = counter.most_common(1)[0][0]
        else:
            temp[asn] = None   # no domain listed for this ASN

    return temp


def process_ipinfo(date_str, date_dash):
    raw_file = find_nearest_file(date_dash)
    if not raw_file:
        print(f"[IPINFO] No ASN CSV found for {date_dash} in {RAW_IPINFO_BASE}")
        return

    actual_date = os.path.basename(raw_file).split(".")[0]
    print(f"[IPINFO] Using file: {raw_file}  (requested {date_dash}, found {actual_date})")

    temp = build_asn_domain_map(raw_file)
    print(f"[IPINFO] Total ASNs in file: {len(temp)}")

    # Reference logic: strip "AS" prefix, keep only string (non-None) values
    ipinfo_as2web = {}
    for asn in temp:
        if isinstance(temp[asn], str):
            ipinfo_as2web[asn[2:]] = temp[asn]

    print(f"[IPINFO] ASes with domain (before clean): {len(ipinfo_as2web)}")

    # Remove unmeaningful domains
    flagged = [asn for asn, dom in ipinfo_as2web.items() if dom in unmeaningful_domain]
    for asn in flagged:
        del ipinfo_as2web[asn]

    print(f"[IPINFO] ASes with domain (after clean):  {len(ipinfo_as2web)}")

    target_dir = os.path.join(OUTPUT_BASE, date_str)
    makedir(target_dir)
    out_path = os.path.join(target_dir, "as2domain.json")
    dump_json(out_path, ipinfo_as2web)
    print(f"[IPINFO] Written to {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date in YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--ipinfo-dir", required=True,
                        help="Directory containing dated .asn.csv.gz files.")
    parser.add_argument("--output-dir", required=True,
                        help="Directory in which date-stamped output folders will be created.")
    args = parser.parse_args()

    global RAW_IPINFO_BASE, OUTPUT_BASE
    RAW_IPINFO_BASE = args.ipinfo_dir
    OUTPUT_BASE = args.output_dir

    date_in = args.date
    if "-" in date_in:
        date_dash = date_in
        date_str = date_in.replace("-", "")
    else:
        date_str = date_in
        date_dash = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    print(f"Running ipinfo pipeline for date {date_dash}")
    process_ipinfo(date_str, date_dash)


if __name__ == "__main__":
    main()
