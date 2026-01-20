import pandas as pd
import json
import argparse
import os

def extract_ipinfo_as2domain(date, input_dir, output_path):
    data = pd.read_csv(f"{input_dir}/ipinfo/{date}/free-2025-01-01.asn.csv")
    data = data.set_index('asn')['domain'].to_dict()
    ipinfo_as2web = {}
    for asn in data:
        ipinfo_as2web[asn[2:]] = data[asn]
    nan_ipinfo_as = []
    for asn in ipinfo_as2web:
        if pd.isna(ipinfo_as2web[asn]):
            nan_ipinfo_as.append(asn)
    for asn in nan_ipinfo_as:
        del ipinfo_as2web[asn]

    print(f"#ASes with websites from IPinfo: {len(ipinfo_as2web)}")
    with open(output_path+"/as2domain.json", "w") as f:
        json.dump(ipinfo_as2web, f, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract AS to website mapping from IPinfo data.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Top-level input data directory (default: ./data)")
    parser.add_argument("--output", default=None, help="Output path for cleaned JSON (default: data/ipinfo/{date}/as2domain.json)")
    args = parser.parse_args()

    output_path = args.output or f"{args.input_dir}/ipinfo/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    extract_ipinfo_as2domain(args.date, args.input_dir, output_path)
