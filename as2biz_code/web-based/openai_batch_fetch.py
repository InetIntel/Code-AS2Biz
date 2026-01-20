#!/usr/bin/env python
from dotenv import load_dotenv
from openai import OpenAI
import os
import time
import json

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def wait_for_batch_completion(batch_id, poll_interval=30):
    print(f"Polling batch job: {batch_id}")
    while True:
        batch = client.batches.retrieve(batch_id)
        print(f"Status: {batch.status}")
        if batch.status in ["completed", "failed", "expired"]:
            return batch
        time.sleep(poll_interval)

def fetch_batch_results(batch_id,
                        jsonl_output="./openai/batch_output.jsonl",
                        formatted_json="./openai/batch_output.json",
                        error_output="./openai/batch_errors.jsonl",
                        task_map_file="./tmp/batch_task_mapping.json",
                        webcat_output="./openai/web_categories.json"):
    batch = wait_for_batch_completion("batch_680643d411888190a7d89c58beaafb74")

    if batch.status != "completed" or not batch.output_file_id:
        print(f"⚠️ No output file available. Batch status: {batch.status}")
        return

    # Step 1: Download raw result file
    output_response = client.files.content(batch.output_file_id)
    output_text = output_response.read().decode("utf-8")

    os.makedirs(os.path.dirname(jsonl_output), exist_ok=True)
    with open(jsonl_output, "w", encoding="utf-8") as f:
        f.write(output_text)
    print(f"✅ Raw output saved to {jsonl_output}")

    results = [json.loads(line) for line in output_text.splitlines() if line.strip()]
    with open(formatted_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"✅ Pretty JSON saved to {formatted_json}")

    # Step 2: Load task-to-website mapping
    if not os.path.exists(task_map_file):
        raise FileNotFoundError(f"❌ Mapping file not found: {task_map_file}")

    with open(task_map_file, "r", encoding="utf-8") as f:
        task_map = json.load(f)

    # Step 3: Generate website → GPT content mapping
    webcat_results = {}
    for item in results:
        try:
            task_id = item["custom_id"]
            website = task_map.get(task_id)
            if not website:
                print(f"⚠️ No website for task_id: {task_id}")
                continue

            # Safely access nested structure
            content = item["response"]["body"]["choices"][0]["message"]["content"]
            webcat_results[website] = content

        except Exception as e:
            print(f"⚠️ Error parsing task {item.get('custom_id', '?')}: {e}")
    with open(webcat_output, "w", encoding="utf-8") as f:
        json.dump(webcat_results, f, indent=2)
    print(f"✅ Website → GPT classification saved to {webcat_output}")

    # Step 4: Fetch errors (optional)
    if batch.error_file_id:
        error_response = client.files.content(batch.error_file_id)
        error_text = error_response.read().decode("utf-8")
        with open(error_output, "w", encoding="utf-8") as f:
            f.write(error_text)
        print(f"⚠️ Errors saved to {error_output}")
    else:
        print("✅ No errors found in batch.")

if __name__ == "__main__":
    batch_id_file = "./tmp/batch_job_id.txt"
    if not os.path.exists(batch_id_file):
        raise FileNotFoundError("Batch job ID file not found. Submit a batch first.")

    with open(batch_id_file, "r") as f:
        batch_id = f.read().strip()

    fetch_batch_results(batch_id)
