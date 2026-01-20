#!/usr/bin/env python
from dotenv import load_dotenv
from openai import OpenAI
import os
import json
import argparse
import sys
import tiktoken
import utils  # You must ensure this has load_json()
from prompts import template_singlemodal, taxonomy, descr, classification_instructions

# Load environment variables and initialize the client.
load_dotenv()
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def count_tokens(text, model="gpt-4o"):
    try:
        encoding = tiktoken.encoding_for_model(model)
    except Exception:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))

def create_classification_tasks(selected_prompts, temp=1, model='gpt-4o'):
    tasks = []
    mapping = {}
    for idx, web in enumerate(selected_prompts):
        message = selected_prompts[web]
        custom_id = f"task-{idx}"
        task = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model,
                "temperature": temp,
                "messages": [{"role": "user", "content": message}, {"role": "developer", "content": classification_instructions}]
            }
        }
        tasks.append(task)
        mapping[custom_id] = web
    return tasks, mapping

def create_batchinp_file(tasks, batch_inpfile):
    with open(batch_inpfile, 'w', encoding='utf-8') as f:
        for obj in tasks:
            f.write(json.dumps(obj) + '\n')
    print(f"✅ Batch input file created at {batch_inpfile} with {len(tasks)} tasks.")

def upload_and_run_batch(batch_inpfile):
    batch_file = client.files.create(
        file=open(batch_inpfile, "rb"),
        purpose="batch"
    )
    batch_job = client.batches.create(
        input_file_id=batch_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    print(f"✅ Batch job submitted. Job ID: {batch_job.id}")
    return batch_job.id

def load_website_data(websites, base_path):
    website_data = [
        {
            "name": website,
            "subpages": sorted(
                [f for f in os.listdir(os.path.join(base_path, website)) if f.endswith(".txt")]
            )
        }
        for website in websites if os.path.isdir(os.path.join(base_path, website))
    ]
    return website_data

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Construct and optionally submit OpenAI batch job for website classification")
    
    parser.add_argument("--base_dir", type=str, default="./html",
                        help="Base directory containing the scraped HTML/text content for each website")
    parser.add_argument("--batch_temp_file", type=str, default="./tmp/batch-temp-file.jsonl",
                        help="Temporary file path to write batch requests")
    parser.add_argument("--task_map_file", type=str, default="./tmp/batch_task_mapping.json",
                        help="Path to save custom_id to website mapping")
    parser.add_argument('--model', type=str, default="gpt-4o", help='Model to use for the query')
    parser.add_argument('--temp', type=float, default=0, help='Temperature value between 0 and 2')
    parser.add_argument('--max_tokens', type=int, default=25000, help='Maximum input token count per website')
    parser.add_argument('--query_num', type=int, default=2, help='Number of websites to query')
    parser.add_argument('--submit_batch', type=lambda x: x.lower() == "true", default=True,
                        help='Whether to submit batch (true/false)')

    args = parser.parse_args()
    BASE_HTML_FOLDER = args.base_dir


    # websites = utils.load_json("./tmp/new_scraped_path.json")
    websites = utils.load_json("./data/dataworks/human_labels.json")
    
    website_txt_data = load_website_data(websites, BASE_HTML_FOLDER)

    output_file = "./openai/final_results.json"
    shared_data = {}
    if os.path.exists(output_file):
        try:
            shared_data = utils.load_json(output_file)
            print(f"Loaded {len(shared_data)} existing responses from {output_file}")
        except Exception:
            print("⚠️ Failed to load prior responses.")

    selected_prompts = {}
    total_tokens = 0
    for web in websites:
        if web in shared_data:
            continue
        subpages = next((w["subpages"] for w in website_txt_data if w["name"] == web), [])
        base_prompt = f"{web}\n\n{template_singlemodal}\n\n{taxonomy}\n\n{descr}\n\nSubpages Content:\n"
        # base_prompt = f"{web}\n\n"
        base_tokens = count_tokens(base_prompt, model=args.model)
        current_tokens = base_tokens
        subpage_texts = []

        for sub in subpages:
            sub_path = os.path.join(BASE_HTML_FOLDER, web, sub)
            try:
                with open(sub_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        continue
                    sub_content = f"[{sub}]\n{content}"
                    sub_tokens = count_tokens(sub_content, model=args.model)
                    if current_tokens + sub_tokens > args.max_tokens:
                        print(f"Skipping subpage {sub} for {web} to stay within token limit.")
                        continue
                    subpage_texts.append(sub_content)
                    current_tokens += sub_tokens
            except Exception as e:
                print(f"⚠️ Failed to read {sub_path}: {e}")

        if subpage_texts:
            full_prompt = base_prompt + "\n\n".join(subpage_texts)
            selected_prompts[web] = full_prompt
            total_tokens += current_tokens
        else:
            print(f"{web} has no usable subpage content.")

    print(f"✅ Selected {len(selected_prompts)} websites. Total token count: {total_tokens}")

    if not selected_prompts:
        print("❌ No prompts generated. Exiting.")
        sys.exit(0)

    # utils.dump_json("./tmp/test_user_prompts1.json", selected_prompts)

    tasks, custom_id_map = create_classification_tasks(selected_prompts, temp=args.temp, model=args.model)
    create_batchinp_file(tasks, args.batch_temp_file)

    with open(args.task_map_file, "w", encoding="utf-8") as f:
        json.dump(custom_id_map, f, indent=2)
    print(f"✅ Task-to-website mapping saved to {args.task_map_file}")

    if args.submit_batch:
        batch_job_id = upload_and_run_batch(args.batch_temp_file)
        os.makedirs("./tmp", exist_ok=True)
        with open("./tmp/batch_job_id.txt", "w") as f:
            f.write(batch_job_id)
        print("✅ Batch job submitted. You can now shut down your device.")
    else:
        print("🛑 Batch not submitted. Only mapping and batch file were saved.")
