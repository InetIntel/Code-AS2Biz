import argparse
import os
import re
from urllib.parse import urlparse
from rapidfuzz import fuzz
import utils

def extract_first_url(text):
    match = re.search(r'\b((?:https?://|www\.)[\w.-]+\.[a-z]{2,}(?:[^\s\)]*))', text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None

def collect_PDB_as2web(date, pdb_dir):
    yyyy, mm, dd = date[:4], date[4:6], date[6:]
    pdb_file = os.path.join(pdb_dir, yyyy, mm, f"peeringdb_2_dump_{yyyy}_{mm}_{dd}.json")
    if not os.path.exists(pdb_file):
        print(f"Warning: {pdb_file} does not exist.")
        return {}
    peeringdb = utils.load_json(pdb_file)
    org, org_web, as2web = {}, {}, {}
    for i in range(len(peeringdb["org"]["data"])):
        org_id = str(peeringdb["org"]["data"][i]["id"])
        country = peeringdb["org"]["data"][i].get("country", "")
        name = peeringdb["org"]["data"][i].get("name", "")
        website = peeringdb["org"]["data"][i].get("website", "")
        aka = peeringdb["org"]["data"][i].get("aka", "")
        if org_id not in org:
            org[org_id] = {}
        url_in_name = extract_first_url(name)
        org[org_id]["name"] = name
        org[org_id]["country"] = country
        if website:
            org[org_id]["website"] = website
        elif url_in_name:
            org[org_id]["website"] = url_in_name
        else:
            org[org_id]["website"] = ""
        org[org_id]["aka"] = aka
        if name not in org_web:
            if website != "":
                org_web[name] = website
    for i in range(len(peeringdb["net"]["data"])):
        asn = str(peeringdb["net"]["data"][i]["asn"])
        website = peeringdb["net"]["data"][i].get("website", "")
        org_id = str(peeringdb["net"]["data"][i]["org_id"])
        if org_id in org:
            org_website = org[org_id]["website"]
            if asn not in as2web:
                as2web[asn] = []
                if website:
                    as2web[asn].append(website)
                if org_website:
                    as2web[asn].append(org_website)
    return as2web

def find_relevant_url(as_name, org_name, urls):
    as_name, org_name = as_name.lower(), org_name.lower()
    url_list = [url.lower() for url in urls]

    scores = []
    for url in url_list:
        as_similarity = fuzz.partial_ratio(url, as_name)
        org_similarity = fuzz.partial_ratio(url, org_name)
        total_score = 0.5 * as_similarity + 0.5 * org_similarity
        scores.append((url, total_score))

    sorted_urls = sorted(scores, key=lambda x: x[1], reverse=True)
    return sorted_urls[0][0] if sorted_urls else None

def clean_url(url: str) -> str:
    url = url.strip().lower()
    parsed = urlparse(url)

    netloc = parsed.netloc
    path = parsed.path.rstrip("/")

    if netloc.startswith("www."):
        netloc = netloc[4:]

    if path:
        return f"{netloc}{path}"
    else:
        return netloc

def main():
    ap = argparse.ArgumentParser(description="Collect candidate PeeringDB URLs per ASN.")
    ap.add_argument("--date", required=True, help="Snapshot date in YYYYMMDD.")
    ap.add_argument("--peeringdb-dir", required=True, help="Directory containing the PeeringDB input snapshot.")
    ap.add_argument("--whois-dir", required=True, help="Directory containing date-stamped RIR info files.")
    ap.add_argument("--output-dir", required=True, help="Directory for this stage's output files.")
    args = ap.parse_args()
    date = args.date
    pdb_dir = args.peeringdb_dir
    whois_dir = args.whois_dir

    pdb_as2web_raw = collect_PDB_as2web(date, pdb_dir)

    rir_list = ["arin", "apnic", "ripe", "lacnic", "afrinic"]
    asn_info = {}

    for rir in rir_list:
        info_path = os.path.join(whois_dir, rir, date, f"{rir}_ca2o_like_info.json")
        try:
            info = utils.load_json(info_path)
            for asn in info:
                try:
                    if "AS" in asn:
                        clean_asn = asn[2:]
                    else:
                        clean_asn = asn

                    if rir != "lacnic":
                        asn_info[clean_asn] = {
                            "AS Name": info[asn].get("as-name", info[asn].get("ASName", "")),
                            "Org Name": info[asn].get("org", info[asn].get("descr", ""))
                        }
                    else:
                        asn_info[clean_asn] = {"AS Name": "", "Org Name": info[asn]}

                except Exception:
                    pass
        except FileNotFoundError:
            print(f"Warning: Whois data not found at {info_path}")

    needed_as_info = {}
    cnt = 0
    pdb_as2web = {}

    for asn in pdb_as2web_raw:
        web_set = set()
        for url in pdb_as2web_raw[asn]:
            web_set.add(clean_url(url))

        if len(web_set) > 1:
            if asn in asn_info:
                asname = asn_info[asn].get("AS Name", "")
                orgname = asn_info[asn].get("Org Name", "")
                url = find_relevant_url(as_name=asname, org_name=orgname, urls=web_set)
                cnt += 1
                pdb_as2web[asn] = url
                needed_as_info[asn] = asn_info[asn]
        else:
            if pdb_as2web_raw[asn]:
                pdb_as2web[asn] = pdb_as2web_raw[asn][0]

    print("#Two different url:", cnt)
    print(len(needed_as_info))

    output_dir = os.path.join(args.output_dir, date)
    os.makedirs(output_dir, exist_ok=True)
    utils.dump_json(os.path.join(output_dir, f"{date}_pdb_needed_as_info.json"), needed_as_info)
    utils.dump_json(os.path.join(output_dir, f"{date}_pdb_as2url_raw.json"), pdb_as2web_raw)
    utils.dump_json(os.path.join(output_dir, f"{date}_asn_names.json"), asn_info)

if __name__ == "__main__":
    main()
