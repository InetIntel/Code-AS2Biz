import json
import argparse
import os
from urllib.parse import urlparse
from tqdm import tqdm
import asyncio
import aiohttp
import time
import gzip

# === Perplexity API Setup ===
API_KEY = "xxx"  # Replace with your actual API key
API_ENDPOINT = "https://api.perplexity.ai/chat/completions"
REQUEST_INTERVAL = 1  # Tier-1: ~60 requests/minute

class RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last_called = 0

    async def wait(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_called
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self._last_called = asyncio.get_event_loop().time()

rate_limiter = RateLimiter(REQUEST_INTERVAL)

def load_json(path):
    with open(path, "r", encoding='latin-1') as f:
        return json.load(f)

async def fetch(session, org, message, shared_dict):
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": "Be precise and concise, ensure thorough web search."},
            {"role": "user", "content": message}
        ],
        "max_tokens": 123,
        "temperature": 0,
        "top_p": 1,
        "search_domain_filter": None,
        "return_images": False,
        "return_related_questions": False,
        "search_recency_filter": "year",
        "top_k": 0,
        "stream": False,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "response_format": None
    }

    await rate_limiter.wait()

    try:
        async with session.post(API_ENDPOINT, json=payload, headers=headers) as response:
            if "application/json" in response.headers.get("Content-Type", ""):
                result = await response.json()
            else:
                print(f"Non-JSON response for '{org}': {await response.text()}")
                return None
    except Exception as e:
        print(f"Error querying '{org}': {e}")
        return None

    # content = result["choices"][0]["message"]["content"] if result and "choices" in result else None
    shared_dict[org] = result
    return result

async def periodic_save(shared_dict, interval, filename):
    while True:
        await asyncio.sleep(interval)
        try:
            with gzip.open(filename, "wt", encoding="utf-8") as f:
                json.dump(shared_dict, f)
            print(f"Saved partial results to {filename} at {time.ctime()}")
        except Exception as e:
            print(f"Error during periodic save: {e}")

async def bulk_query(orgs, messages, shared_dict):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch(session, org, msg, shared_dict) for org, msg in zip(orgs, messages)]
        return await asyncio.gather(*tasks, return_exceptions=True)

async def main(args):
    as2web = load_json(f"{args.input_dir}/as_centered_sources/{args.date}/as_centered_as2web.json")
    as2name_cc = load_json(f"{args.input_dir}/as_centered_sources/{args.date}/as2name_cc.json")
    no_web_as = set(load_json(f"{args.input_dir}/as_centered_sources/{args.date}/as_centered_noweb_as.json"))
    inaccessible_web_as = {asn for asn, val in as2web.items() if not val["Accessible"]}
    to_query_asns = sorted(no_web_as.union(inaccessible_web_as))

    print(f"Total ASNs to query: {len(to_query_asns)}")

    messages = [
        f"Based on the organization's name and the country code of its registered country, provide its website or social media page URL: {as2name_cc.get(asn)}. The URL must contain the exact registered name or a clear, verifiable connection to it. If multiple organizations in the same country share a similar name without a way to uniquely identify the correct one, or if no website is associated with the organization, respond with 'No match.'. Your response should contain only the identified URL or simply 'No match.'"
        for asn in to_query_asns
    ]

    output_dir = args.output or f"{args.input_dir}/perplexity_ai/{args.date}"
    os.makedirs(output_dir, exist_ok=True)
    final_filename = f"{output_dir}/sonar_pro_responses_final.json.gz"
    partial_filename = f"{output_dir}/sonar_pro_responses_partial.json.gz"

    shared_data = {}
    if os.path.exists(final_filename):
        try:
            with gzip.open(final_filename, "rt", encoding="latin-1") as f:
                shared_data = json.load(f)
            print(f"Loaded {len(shared_data)} existing responses from {final_filename}")
        except Exception as e:
            print(f"Error loading saved responses: {e}")

    to_query = [asn for asn in to_query_asns if asn not in shared_data]
    query_messages = [msg for asn, msg in zip(to_query_asns, messages) if asn not in shared_data]

    print(f"{len(to_query)} queries to run")

    save_task = asyncio.create_task(periodic_save(shared_data, interval=30, filename=partial_filename))
    start_time = time.time()
    await bulk_query(to_query, query_messages, shared_data)
    print(f"Finished queries in {time.time() - start_time:.2f} seconds.")

    save_task.cancel()
    try:
        await save_task
    except asyncio.CancelledError:
        pass

    with gzip.open(final_filename, "wt", encoding="latin-1") as f:
        json.dump(shared_data, f, indent=2)
    print(f"Final results saved to {final_filename}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query Perplexity AI for ASes with missing or inaccessible websites.")
    parser.add_argument("--date", required=True, help="Date in yymmdd format (e.g., 250101)")
    parser.add_argument("--input_dir", default="data", help="Input directory (default: ./data)")
    parser.add_argument("--output", default=None, help="Output path for JSON (default: ./data/perplexity_ai/{date})")
    args = parser.parse_args()

    asyncio.run(main(args))
