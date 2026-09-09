import os
import json
import argparse
from rapidfuzz import fuzz

unmeaningful_domain = ["gmail.com", "yahoo.com", "me.com", "hotmail.com", "lacnic.net", "ripe.net", "apnic.net", "mhs.attmail.com"]
INPUT_BASE = None
WHOIS_BASE = None


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def dump_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def makedir(path):
    os.makedirs(path, exist_ok=True)


def find_relevant_domain(as_name, org_name, domains):
    """
    Given a list of candidate domains, pick the one most similar to the AS name
    and org name using fuzzy string matching. Mirrors the arin_afrinic logic.
    When as_name / org_name are unknown, pass empty strings; all domains will tie
    and the first element (after stable sort) is returned.
    """
    as_name = as_name.lower()
    org_name = org_name.lower()
    domains = [d.lower() for d in domains]

    scores = []
    for domain in domains:
        as_similarity = fuzz.partial_ratio(domain, as_name)
        org_similarity = fuzz.partial_ratio(domain, org_name)
        total_score = 0.5 * as_similarity + 0.5 * org_similarity
        scores.append((domain, total_score))

    sorted_domains = sorted(scores, key=lambda x: x[1], reverse=True)
    return sorted_domains[0][0] if sorted_domains else None


def extract_domains(emails):
    """Extract unique lowercase domains from a list of email addresses."""
    domains = []
    seen = set()
    for email in emails:
        if "@" in email:
            domain = email.split("@")[-1].lower()
            if domain not in seen:
                seen.add(domain)
                domains.append(domain)
    return domains


def filter_unmeaningful(as2domain, rir_tag):
    """Remove entries whose domain appears in the unmeaningful list."""
    flagged = [asn for asn, dom in as2domain.items() if dom in unmeaningful_domain]
    print(f"[{rir_tag}] ASes with Whois domain (before clean): {len(as2domain)}")
    for asn in flagged:
        del as2domain[asn]
    print(f"[{rir_tag}] ASes with Whois domain (after clean): {len(as2domain)}")


# ---------------------------------------------------------------------------
# Per-RIR processors
# ---------------------------------------------------------------------------

def process_ripe(date_str):
    """
    Input: {date_str}_ripe_asn2email.json
      dict  key   -> "ASxxx"  (e.g. "AS3255")
            value -> list of email addresses (usually one element)

    Output: WHOIS_BASE/ripe/{date_str}/as2domain.json
      dict  key -> ASN without "AS" prefix  (e.g. "3255")
            value -> domain string
    """
    input_file = os.path.join(INPUT_BASE, date_str, f"{date_str}_ripe_asn2email.json")
    if not os.path.exists(input_file):
        print(f"[RIPE] Input file not found: {input_file}")
        return

    data = load_json(input_file)
    print(f"[RIPE] Loaded {len(data)} ASN entries from {input_file}")

    as2domain = {}
    no_domain = []

    for asn_key, emails in data.items():
        asn = asn_key[2:] if asn_key.upper().startswith("AS") else asn_key

        domains = extract_domains(emails)
        if not domains:
            no_domain.append(asn)
            continue

        if len(domains) == 1:
            as2domain[asn] = domains[0]
        else:
            # Multiple distinct domains: use fuzzy matching (APNIC method).
            # We have no org name here, so use the ASN string for both parameters.
            domain = find_relevant_domain(asn, asn, domains)
            as2domain[asn] = domain

    print(f"[RIPE] #ASes with domain: {len(as2domain)}  #no domain: {len(no_domain)}")
    filter_unmeaningful(as2domain, "RIPE")

    target_dir = os.path.join(WHOIS_BASE, "ripe", date_str)
    makedir(target_dir)
    dump_json(os.path.join(target_dir, "as2domain.json"), as2domain)
    print(f"[RIPE] Written to {target_dir}/as2domain.json")


def process_lacnic(date_str):
    """
    Input: {date_str}_lacnic_as2email.json
      dict  key   -> ASN as plain string  (e.g. "278")
            value -> dict with "emails" field (list of email addresses, usually one)

    Output: WHOIS_BASE/lacnic/{date_str}/as2domain.json
      dict  key -> ASN string  (e.g. "278")
            value -> domain string
    """
    input_file = os.path.join(INPUT_BASE, date_str, f"{date_str}_lacnic_as2email.json")
    if not os.path.exists(input_file):
        print(f"[LACNIC] Input file not found: {input_file}")
        return

    data = load_json(input_file)
    print(f"[LACNIC] Loaded {len(data)} ASN entries from {input_file}")

    as2domain = {}
    no_domain = []

    for asn, entry in data.items():
        emails = entry.get("emails", [])

        domains = extract_domains(emails)
        if not domains:
            no_domain.append(asn)
            continue

        if len(domains) == 1:
            as2domain[asn] = domains[0]
        else:
            # Multiple distinct domains: use fuzzy matching (APNIC method).
            domain = find_relevant_domain(asn, asn, domains)
            as2domain[asn] = domain

    print(f"[LACNIC] #ASes with domain: {len(as2domain)}  #no domain: {len(no_domain)}")
    filter_unmeaningful(as2domain, "LACNIC")

    target_dir = os.path.join(WHOIS_BASE, "lacnic", date_str)
    makedir(target_dir)
    dump_json(os.path.join(target_dir, "as2domain.json"), as2domain)
    print(f"[LACNIC] Written to {target_dir}/as2domain.json")


def process_apnic(date_str):
    """
    Input: {date_str}_apnic_as2email.json
      dict  key   -> ASN as plain string  (e.g. "173")
            value -> list of email addresses (often multiple)

    Output: WHOIS_BASE/apnic/{date_str}/as2domain.json
      dict  key -> ASN string  (e.g. "173")
            value -> domain string

    APNIC entries routinely carry multiple emails / domains, so we always apply
    fuzzy matching (same find_relevant_domain approach used by arin_afrinic).
    """
    input_file = os.path.join(INPUT_BASE, date_str, f"{date_str}_apnic_as2email.json")
    if not os.path.exists(input_file):
        print(f"[APNIC] Input file not found: {input_file}")
        return

    data = load_json(input_file)
    print(f"[APNIC] Loaded {len(data)} ASN entries from {input_file}")

    as2domain = {}
    no_domain = []

    for asn, emails in data.items():
        domains = extract_domains(emails)
        if not domains:
            no_domain.append(asn)
            continue

        # Always use fuzzy matching; for a single domain it degenerates to
        # returning that domain trivially.
        domain = find_relevant_domain(asn, asn, domains)
        as2domain[asn] = domain

    print(f"[APNIC] #ASes with domain: {len(as2domain)}  #no domain: {len(no_domain)}")
    filter_unmeaningful(as2domain, "APNIC")

    target_dir = os.path.join(WHOIS_BASE, "apnic", date_str)
    makedir(target_dir)
    dump_json(os.path.join(target_dir, "as2domain.json"), as2domain)
    print(f"[APNIC] Written to {target_dir}/as2domain.json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date in YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--input-dir", required=True, help="Root containing date-stamped RIR email JSON files.")
    parser.add_argument("--output-dir", required=True, help="Root for generated RIR/date AS2domain files.")
    args = parser.parse_args()

    global INPUT_BASE, WHOIS_BASE
    INPUT_BASE = args.input_dir
    WHOIS_BASE = args.output_dir

    date_in = args.date
    if "-" in date_in:
        date_str = date_in.replace("-", "")
    else:
        date_str = date_in

    print(f"Running lacnic_ripe_apnic pipeline for date {date_str}")
    process_ripe(date_str)
    process_lacnic(date_str)
    process_apnic(date_str)


if __name__ == "__main__":
    main()
