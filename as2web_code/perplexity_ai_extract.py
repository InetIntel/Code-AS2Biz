import gzip
import json
import argparse
from utils import find_relevant_domain
import requests
from urllib.parse import urlparse
import os
from tqdm import tqdm
import re

def load_json(path):
    with open(path, "r", encoding='latin-1') as f:
        return json.load(f)

def extract_first_url(text):
    # First, try to match markdown formatted URL (e.g., [text](URL))
    match = re.search(r'\[.*?\]\((https?://[^\)]+)\)', text)
    if match:
        return match.group(1)
    
    # Second, try to match URLs that start with http(s):// or www.
    match = re.search(r'((?:https?://|www\.)[\w\.-]+\.[a-z]{2,}(?:[^\s\)]*))', text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Third, fallback to matching a plain domain name (e.g., buenasnoticiasqr.com)
    # This regex matches one or more domain components followed by a TLD.
    match = re.search(r'\b((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})\b', text)
    if match:
        return match.group(1)
    
    return None

def postprocess_url(raw_url):
    """
    Extracts a clean URL from a raw string that might contain trailing markdown references, asterisks, or punctuation.
    """
    # Match full URL including path, query, fragments — until it hits an obvious non-URL character
    match = re.search(r'(https?://[^\s\]\)\*]+|www\.[^\s\]\)\*]+)', raw_url)
    if match:
        url = match.group(1)
        # Strip trailing punctuation or markdown syntax
        url = re.sub(r'[\*\)\]\.,:;!?]+$', '', url)
        return url
    return raw_url

def extract_clean_domain(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.split(':')[0]  # Remove port if exists


def combine_as2web(date, input_dir, output_path):
    as2web = load_json(f"{input_dir}/as_centered_sources/{date}/as_centered_as2web.json")
    
    as2name_cc = load_json(f"{args.input_dir}/as_centered_sources/{args.date}/as2name_cc.json")
    name2as = {}
    for asn in as2name_cc:
        name = as2name_cc[asn]
        name2as.setdefault(name, [])
        name2as[name].append(asn)
    with gzip.open(f"{input_dir}/perplexity_ai/{date}/sonar_pro_responses_final.json.gz", "rt", encoding="latin-1") as f:
        sonar_pro_data = json.load(f)
    none_org, no_url_ans_org = [], [] # Might need re-query
    for org in sonar_pro_data:
        result = sonar_pro_data[org]
        ans = result["choices"][0]["message"]["content"] if result and "choices" in result else None
        if ans:
            if "No match" in ans:
                continue
            else:
                url = extract_first_url(ans)
                if url:
                    website = postprocess_url(url)
                    for asn in name2as[org]:
                        if asn not in as2web: # No website based on AS-centered sources
                            as2web[asn] = {"Website": website, "Sources": ["Perplexity AI sonar-pro"]}
                        else:
                            as_centered_url = as2web[asn]["Website"]
                            if extract_clean_domain(website) != extract_clean_domain(as_centered_url): # Different website
                                as2web[asn] = {"Website": website, "Sources": ["Perplexity AI sonar-pro"]}
                else:
                    no_url_ans_org.append(org)
        else:
            none_org.append(org)

    with open(output_path+"/as2web.json", "w") as f:
        json.dump(as2web, f, indent=2)
    print(f"Saved cleaned mapping to {output_path}")
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Combine Perplexity AI and AS-centered sources.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Input directory (default: ./data)")
    parser.add_argument("--output", default=None, help="Output path for JSON (default: ./data/as2web/{date})")
    args = parser.parse_args()
    output_path = args.output or f"{args.input_dir}/as2web/{args.date}"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    combine_as2web(args.date, args.input_dir, output_path)
