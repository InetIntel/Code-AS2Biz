import utils

no_type_as = utils.load_json("./result/none_as_cb.json")
as2web = utils.load_json("./data/final_as2web.json")
ca2o = utils.load_json("./data/ca2o/20250101/CA2O_as2org_name.json")
both_cb_as = utils.load_json("./result/both_as_cb.json")
direct_cb_as = utils.load_json("./result/only_direct_as_cb.json")
inherit_cb_as = utils.load_json("./result/only_inherit_as_cb.json")

import tldextract
import re

# Common suffixes (case-insensitive)
SUFFIXES = [
    r"llc", r"inc", r"corp", r"ltd", r"gmbh", r"s\.a\.", r"s\.p\.a\.", r"co(.,)? ltd\.?",
    r"bv", r"pjsc", r"plc", r"limited", r"ag", r"nv", r"oy", r"ab", r"sa", r"sarl"
]

# Compile a regex pattern
SUFFIX_PATTERN = re.compile(r"\b(" + "|".join(SUFFIXES) + r")\b", flags=re.IGNORECASE)

def extract_brand_name(org_name):
    # Remove commas and dots
    cleaned = re.sub(r"[.,]", "", org_name)
    # Remove common suffixes
    cleaned = SUFFIX_PATTERN.sub("", cleaned)
    # Collapse multiple spaces
    return re.sub(r"\s+", " ", cleaned).strip()

wiki_search_asinfo = {}

def extract_base_name(domain):
    ext = tldextract.extract(domain)
    # ext.domain is the main domain (e.g. 'lan' from lan.co.uk)
    return ext.domain.capitalize() if ext.domain else domain.capitalize()


def extract_org_and_country(full_string):
    parts = full_string.strip().split()
    if len(parts) < 3:
        raise ValueError("Input must contain at least three words.")
    
    org_name = " ".join(parts[:-2])  # all except last two words
    country = parts[-2]              # second-to-last word
    return org_name, country

cnt = 0
as2country = {}
for asn in no_type_as:
    org_name = ca2o.get(asn)
    if org_name:
        org_name, country = extract_org_and_country(org_name)
        as2country[asn] = country
        brand = extract_brand_name(org_name)
        if brand:
            wiki_search_asinfo[asn] = brand
    cnt += 1
print(len(wiki_search_asinfo))
print(len(as2country))

cnt = 0
as2country1 = {}
wiki_search_asinfo1 = {}
for asn in cb_obtained_as:
    org_name = ca2o.get(asn)
    if org_name:
        org_name, country = extract_org_and_country(org_name)
        as2country1[asn] = country
        brand = extract_brand_name(org_name)
        if brand:
            wiki_search_asinfo1[asn] = brand
    cnt += 1
print(len(wiki_search_asinfo1))
print(len(as2country1))

import requests
import json
from difflib import SequenceMatcher
import pycountry

# Load aliases JSON
with open("./wiki/country_aliases.json", "r", encoding="utf-8") as f:
    COUNTRY_ALIASES = json.load(f)

def fuzzy_match_score(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def get_country_aliases(code):
    code = code.upper()
    aliases = COUNTRY_ALIASES.get(code, [])
    
    # Fallback to official country name
    try:
        country = pycountry.countries.get(alpha_2=code)
        if country:
            aliases.append(country.name)
    except:
        pass
    
    return [alias.lower() for alias in aliases]

def description_contains_country(description, country_code):
    if not country_code:
        return False
    desc_lower = description.lower()
    for alias in get_country_aliases(country_code):
        if alias in desc_lower:
            return True
    return False

import requests
import html2text

def get_wikipedia_full_text(title):
    endpoint = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "parse",
        "page": title,
        "format": "json",
        "prop": "text",
        "redirects": 1  # 🚨 make sure redirects are followed
    }
    response = requests.get(endpoint, params=params)
    if response.status_code != 200:
        return ""
    data = response.json()
    html = data.get("parse", {}).get("text", {}).get("*", "")
    if not html:
        return ""

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_tables = True
    h.body_width = 0
    return h.handle(html).strip()


def search_and_validate_wikipedia(query):
    # Step 1: opensearch
    query = query.lstrip('"').rstrip('"')
    endpoint = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "opensearch",
        "search": query,
        "limit": 1,
        "namespace": 0,
        "format": "json"
    }

    response = requests.get(endpoint, params=params)
    if response.status_code != 200:
        print(f"Request failed for query: {query}")
        return None

    data = response.json()
    if not data[1]:
        return None

    title = data[1][0]
    page_url = data[3][0]
    # print(query, page_url)
    # Step 2: fetch full text
    full_text = get_wikipedia_full_text(title)
    if not full_text:
        return None
    # print(full_text)
    
    # print(query.lower())
    # print(query.lower() in full_text.lower())
    # raise
    # Step 3: check query token in text
    if query.lower() not in full_text.lower():
        return None

    # # Step 4: check country alias match
    # if country_code and not description_contains_country(full_text, country_code):
    #     return None

    # Passed all checks
    return {
        "title": title,
        "url": page_url,
        "full_text": full_text
    }
matched_brandinfo = {}
unmatched_brand = []

exact_match_asns = []
fuzzy_match_asns = []

cnt = 0
for query in brands: 
    cnt += 1
    result = search_and_validate_wikipedia(query)
    if result:
        matched_brandinfo[query] = result
    else:
        unmatched_brand.append(result)
    if cnt % 10 == 9:
        print(cnt)
        # break
    # if cnt == 20:
    #     break
    if cnt % 100 == 99:
        print(f"✅ Total matched ASNs: {len(matched_brandinfo)}")
        print(f"❌ Total unmatched ASNs: {len(unmatched_brand)}")
        utils.dump_json("./wiki/matched_results.json", matched_brandinfo)
        utils.dump_json("./wiki/unmatched_results.json", unmatched_brand)

# ✅ Summary
print(f"✅ Total matched ASNs: {len(matched_brandinfo)}")
print(f"❌ Total unmatched ASNs: {len(unmatched_brand)}")
