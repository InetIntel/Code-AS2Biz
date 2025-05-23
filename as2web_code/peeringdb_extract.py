import re
import json
import argparse
import os

# Extract website URL from org-name (some organizations put URLs in their names)
def extract_url_in_name(text):
    match = re.search(r'\b((?:https?://|www\.)[\w.-]+\.[a-z]{2,}(?:[^\s\)]*))', text, re.IGNORECASE)

    if match:
        return match.group(1)
    return None

def extract_peeringdb_as2web(date, input_dir, output_path):
    with open(f"{input_dir}/peeringdb/{date}/peeringdb_2_dump_20{date[:2]}_{date[2:4]}_{date[4:]}.json", encoding="latin-1") as f:
        peeringdb = json.load(f)
    org, org_web, as2web = {}, {}, {}
    for i in range(len(peeringdb["org"]["data"])):
        org_id = str(peeringdb["org"]["data"][i]["id"])
        name = peeringdb["org"]["data"][i]["name"]
        website = peeringdb["org"]["data"][i]["website"]
        if org_id not in org:
            org[org_id] = {}
        url_in_name = extract_url_in_name(name)
        org[org_id]["name"] = name
        if website:
            org[org_id]["website"] = website
        elif url_in_name:
            org[org_id]["website"] = url_in_name
        else:
            org[org_id]["website"] = ""
        if name not in org_web:
            if website != "":
                org_web[name] = website
    
    for i in range(len(peeringdb["net"]["data"])):
        asn = str(peeringdb["net"]["data"][i]["asn"])
        website = peeringdb["net"]["data"][i]["website"]
        org_id = str(peeringdb["net"]["data"][i]["org_id"])
        if org_id in org:
            org_website = org[org_id]["website"]
            if asn not in as2web:
                if website or org_website:
                    as2web[asn] = []
                    if website:
                        as2web[asn].append(website)
                    if org_website and org_website not in as2web[asn]:
                        as2web[asn].append(org_website)

    with open(output_path+"/as2websites.json", "w") as f:
        json.dump(as2web, f, indent=2)
    print(f"Saved cleaned mapping to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract AS to website mapping from PeeringDB data.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Top-level input data directory (default: ./data)")
    parser.add_argument("--output", default=None, help="Output path for cleaned JSON (default: data/peeringdb/{date})")
    args = parser.parse_args()

    output_path = args.output or f"{args.input_dir}/peeringdb/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    extract_peeringdb_as2web(args.date, args.input_dir, output_path)
