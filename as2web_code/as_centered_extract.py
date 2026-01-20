import json
import argparse
from utils import find_relevant_domain
import requests
from urllib.parse import urlparse
import os
from tqdm import tqdm


def load_json(dir):
    with open(dir, "r", encoding='latin-1') as f:
        data = json.load(f)
    return data


def extract_clean_domain(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.split(':')[0]  # Remove port if exists


def check_accessibility(url):
    url = url.strip().lower()
    clean_domain = url

    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
    }

    # Try HTTPS first
    try:
        response = requests.get(url, headers=headers, timeout=5, stream=True)
        if response.status_code < 400:
            return url, True
    except requests.RequestException:
        pass

    # Fallback to HTTP
    http_url = url.replace("https://", "http://", 1)
    try:
        response = requests.get(http_url, headers=headers, timeout=5, stream=True)
        if response.status_code < 400:
            return http_url, True
    except requests.RequestException:
        pass

    return clean_domain, False


def collect_as2web(date, input_dir, output_path):
    arin_as2domains = load_json(f"{input_dir}/arin/{date}/as2domains.json")
    afrinic_as2domains = load_json(f"{input_dir}/afrinic/{date}/as2domains.json")
    ripe_as2domains = load_json(f"{input_dir}/ripe/{date}/as2domains.json")
    apnic_as2domains = load_json(f"{input_dir}/apnic/{date}/as2domains.json")
    lacnic_as2domains = load_json(f"{input_dir}/lacnic/{date}/as2domains.json")
    pdb_as2websites = load_json(f"{input_dir}/peeringdb/{date}/as2websites.json")
    ipinfo_as2domain = load_json(f"{input_dir}/ipinfo/{date}/as2domain.json")
    arin_as_info = load_json(f"{input_dir}/arin/{date}/as_info.json")
    afrinic_as_info = load_json(f"{input_dir}/afrinic/{date}/as_info.json")
    ripe_as_info = load_json(f"{input_dir}/ripe/{date}/as_info.json")
    apnic_as_info = load_json(f"{input_dir}/apnic/{date}/as_info.json")
    lacnic_as_info = load_json(f"{input_dir}/lacnic/{date}/as_info.json")
    scope_rir = load_json(f"{input_dir}/scope/{date}/scope_rir.json")

    as_centered_as2web = {}
    no_web_as = []
    as2name_cc = {}
    for cnt, asn in enumerate(tqdm(scope_rir, desc="Processing ASes"), start=1):
        domain_source_dict = {}
        rir_domain_map = {"arin": arin_as2domains, "apnic": apnic_as2domains, "ripe": ripe_as2domains, 
                        "afrinic": afrinic_as2domains, "lacnic": lacnic_as2domains}
        rir_info_map = {"arin": arin_as_info, "apnic": apnic_as_info, "ripe": ripe_as_info, 
                        "afrinic": afrinic_as_info, "lacnic": lacnic_as_info}
        rir = scope_rir[asn]
        whois_domains = rir_domain_map[rir].get(asn)
        if whois_domains:
            domain_source_dict.setdefault(asn, {})
            for domain in whois_domains:
                domain_source_dict[asn][domain] = ["Whois"] 
        ipinfo_domain = ipinfo_as2domain.get(asn)
        if ipinfo_domain:
            domain_source_dict.setdefault(asn, {})
            if ipinfo_domain in domain_source_dict[asn]:
                domain_source_dict[asn][ipinfo_domain].append("IPinfo")
            else:
                domain_source_dict[asn][domain] = ["IPinfo"] 
        peeringdb_webs = pdb_as2websites.get(asn)
        if peeringdb_webs:
            domain_source_dict.setdefault(asn, {})
            for url in peeringdb_webs:
                url = extract_clean_domain(url)
                if url in domain_source_dict:
                    domain_source_dict[asn][url].append("PeeringDB")
                else:
                    domain_source_dict[asn][url] = ["PeeringDB"] 
        info = rir_info_map[rir]
        asname = info.get(asn).get("asname")
        orgname = info.get(asn).get("orgname")
        cc = info.get(asn).get("country")
        if asn in domain_source_dict:
            most_relevant_domain = find_relevant_domain(asname, orgname, list(domain_source_dict[asn].keys()))
            url, accessibility = check_accessibility(most_relevant_domain)
            if accessibility:
                as_centered_as2web[asn] = {"Website": url, "Sources": domain_source_dict[asn][most_relevant_domain], "Accessible": True}
            else:
                as_centered_as2web[asn] = {"Website": url, "Sources": domain_source_dict[asn][most_relevant_domain], "Accessible": False}
        else:
            no_web_as.append(asn)
        as2name_cc[asn] = orgname + f" ({cc})"
        if cnt > 4:
            break
    with open(output_path+"/as_centered_as2web.json", "w") as f:
        json.dump(as_centered_as2web, f, indent=2)
    with open(output_path+"/as_centered_noweb_as.json", "w") as f:
        json.dump(no_web_as, f, indent=2)
    with open(output_path+"/as2name_cc.json", "w") as f:
        json.dump(as2name_cc, f, indent=2)
    print(f"Saved cleaned mapping to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract AS2Web from AS-centered sources.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Top-level input data directory (default: ./data)")
    parser.add_argument("--output", default=None, help="Output path for cleaned JSON (default: data/as_centered_sources/{date})")
    args = parser.parse_args()

    output_path = args.output or f"{args.input_dir}/as_centered_sources/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    collect_as2web(args.date, args.input_dir, output_path)
