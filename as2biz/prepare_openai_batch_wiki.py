#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wikipedia → OpenAI Batch: build JSONL, chunk, optional submit, optional wait/download/map.

Examples:
  # Build + submit only (poll/download later)
  python prepare_openai_batch_wiki.py --wiki-json ./result/.../wiki_info.json --submit-batch true

  # Build + submit + wait + map to ASNs (one command)
  python prepare_openai_batch_wiki.py --wiki-json ./result/.../wiki_info.json \\
      --submit-batch true --wait --asn2brand-json ./result/.../classifiable_as2brand.json \\
      --output-dir ./results_wiki

  # Only download completed batches + map (jobs already submitted)
  python prepare_openai_batch_wiki.py --fetch-results-only \\
      --jobs-json ./tmp/wiki_batch_jobs.json \\
      --asn2brand-json ./result/.../classifiable_as2brand.json \\
      --output-dir ./results_wiki --wait
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import tiktoken

try:
    import utils

    def load_json_safe(path):
        return utils.load_json(str(path))
except ImportError:
    def load_json_safe(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

try:
    from dotenv import load_dotenv
    from openai import OpenAI
except ImportError:
    print("⚠️  Warning: 'openai' or 'python-dotenv' not installed. Submission will be disabled.")
    OpenAI = None
    load_dotenv = lambda: None

load_dotenv()

MAX_REQUESTS_PER_BATCH = 50_000
MAX_BYTES_PER_BATCH = 190 * 1024 * 1024
DEFAULT_TOKENS_PER_BATCH_CAP = 180_000_000

try:
    from prompt import (
        template_singlemodal_wiki,
        taxonomy,
        descr,
        classification_wiki_instructions,
    )
    HAVE_PROMPT_TEMPLATE = True
except ImportError:
    print("⚠️  Warning: 'prompt' module not found. Using minimal fallback.")
    HAVE_PROMPT_TEMPLATE = False
    template_singlemodal_wiki = ""
    taxonomy = ""
    descr = ""
    classification_wiki_instructions = (
        "You are tasked with classifying organizations based on their Wikipedia articles."
    )


def count_tokens(text: str, model: str = "gpt-5.2") -> int:
    try:
        encoding = tiktoken.encoding_for_model(model)
    except Exception:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def truncate_text(text: str, max_tokens: int, model: str = "gpt-5.2") -> str:
    try:
        encoding = tiktoken.encoding_for_model(model)
    except Exception:
        encoding = tiktoken.get_encoding("cl100k_base")
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[:max_tokens])


def plan_batch_chunks(batch_tasks_lines, tokens_per_batch_cap):
    chunks = []
    current_tasks = []
    current_tokens = 0
    current_bytes = 0

    for item in batch_tasks_lines:
        line = item["line"]
        tokens = item["tokens"]
        line_bytes = len(line.encode("utf-8")) + 1

        if current_tasks and (
            len(current_tasks) >= MAX_REQUESTS_PER_BATCH
            or current_bytes + line_bytes > MAX_BYTES_PER_BATCH
            or current_tokens + tokens > tokens_per_batch_cap
        ):
            chunks.append({
                "tasks": current_tasks,
                "est_tokens": current_tokens,
                "est_bytes": current_bytes,
            })
            current_tasks = []
            current_tokens = 0
            current_bytes = 0

        current_tasks.append(line)
        current_tokens += tokens
        current_bytes += line_bytes

    if current_tasks:
        chunks.append({
            "tasks": current_tasks,
            "est_tokens": current_tokens,
            "est_bytes": current_bytes,
        })
    return chunks


def upload_and_run_batch(client, batch_inpfile: Path):
    if not batch_inpfile.exists():
        print(f"❌ File not found: {batch_inpfile}")
        return None

    print(f"Uploading {batch_inpfile.name} ({batch_inpfile.stat().st_size/1024:.1f} KB)...")
    try:
        batch_file = client.files.create(file=batch_inpfile.open("rb"), purpose="batch")
        print(f"Submitting Batch (File ID: {batch_file.id})...")
        batch_job = client.batches.create(
            input_file_id=batch_file.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )
        print(f"✅ Job Submitted. ID: {batch_job.id}")
        return batch_job.id
    except Exception as e:
        print(f"❌ Submission Failed: {e}")
        return None


def wiki_jobs_log_path(batch_jsonl_path: Path) -> Path:
    return batch_jsonl_path.parent / "wiki_batch_jobs.json"


def persist_wiki_jobs(job_log_path: Path, jobs: dict) -> None:
    existing = {}
    if job_log_path.exists():
        try:
            with open(job_log_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(jobs)
    job_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(job_log_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)


def flatten_batch_response_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "output_text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def extract_wiki_response_text(row: dict) -> str:
    err = row.get("error")
    if err:
        return ""

    response = row.get("response") or {}
    body = response.get("body") or {}

    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        return flatten_batch_response_content(msg.get("content"))

    output = body.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            c = item.get("content")
            if isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and isinstance(x.get("text"), str):
                        parts.append(x["text"])
        if parts:
            return "".join(parts).strip()

    return ""


def get_job_status(client, job_id):
    try:
        return client.batches.retrieve(job_id)
    except Exception as e:
        print(f"❌ Error checking job {job_id}: {e}")
        return None


def download_result(client, file_id, save_path: Path):
    print(f"⬇️  Downloading output file {file_id} to {save_path} ...")
    try:
        fc = client.files.content(file_id)
        text = fc.text if hasattr(fc, "text") else fc.read().decode("utf-8", errors="replace")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return False


def wait_and_download_batches(client, jobs_map: dict, output_dir: Path, wait: bool, poll_interval: int):
    output_dir.mkdir(parents=True, exist_ok=True)
    completed_paths = set()

    while True:
        all_done = True
        pending_jobs = 0
        had_api_failure = False

        print(f"\n--- Checking status at {time.strftime('%H:%M:%S')} ---")

        for fname, job_id in jobs_map.items():
            result_path = output_dir / f"result_{fname}"

            if result_path.exists():
                print(f"✅ {fname}: already downloaded.")
                completed_paths.add(result_path)
                continue

            batch = get_job_status(client, job_id)
            if batch is None:
                had_api_failure = True
                all_done = False
                continue

            status = batch.status
            print(f"⏳ {fname} ({job_id}): {status.upper()}")

            if status == "completed":
                if batch.output_file_id:
                    if download_result(client, batch.output_file_id, result_path):
                        completed_paths.add(result_path)
                else:
                    print(f"⚠️  {fname} completed but output_file_id is missing.")
            elif status in ("failed", "expired", "cancelled"):
                print(f"❌ {fname} ended with status: {status}")
                if getattr(batch, "errors", None):
                    print(f"   Errors: {batch.errors}")
            else:
                all_done = False
                pending_jobs += 1

        if had_api_failure:
            all_done = False

        if all_done:
            print("\n🎉 All jobs reached a terminal state (completed / failed / expired / cancelled).")
            break

        if not wait:
            print("\n⚠️  Some jobs still running. Re-run with --wait or use --fetch-results-only later.")
            break

        print(f"Sleeping {poll_interval}s... ({pending_jobs} job(s) still in progress)")
        time.sleep(poll_interval)

    return sorted(completed_paths)


def parse_and_map_results(result_files, asn2brand_file, output_dir: Path):
    print(f"\nLoading ASN→brand mapping: {asn2brand_file} ...")
    with open(asn2brand_file, encoding="utf-8") as f:
        asn2brand = json.load(f)

    brand_results = {}
    total_responses = 0
    failed_responses = 0

    print("Parsing batch result files...")
    for r_file in result_files:
        print(f"  - Reading {r_file.name} ...")
        with open(r_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue

                custom_id = row.get("custom_id")
                if not custom_id or not str(custom_id).startswith("wiki:"):
                    continue

                brand = str(custom_id).split("wiki:", 1)[1]
                text = extract_wiki_response_text(row)
                if text:
                    brand_results[brand] = text
                    total_responses += 1
                else:
                    failed_responses += 1

    print(f"   > Loaded classifications for {len(brand_results)} unique brand(s).")
    if failed_responses > 0:
        print(f"   > ⚠️ Empty/failed LLM rows: {failed_responses}")

    asn_final_results = {}
    mapped_count = 0
    unmapped_count = 0
    brand_fanout = defaultdict(int)

    for asn, brand in asn2brand.items():
        if brand in brand_results:
            asn_final_results[asn] = {
                "asn": asn,
                "brand": brand,
                "classification": brand_results[brand],
            }
            mapped_count += 1
            brand_fanout[brand] += 1
        else:
            unmapped_count += 1

    print("\n✅ Mapping complete.")
    print(f"   Total ASNs in input map:       {len(asn2brand)}")
    print(f"   ASNs with classification:      {mapped_count}")
    print(f"   ASNs missing classification:   {unmapped_count}")

    print("\nTop 10 brands by ASN coverage:")
    for brand, count in sorted(brand_fanout.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {brand}: {count} ASNs")

    json_out = output_dir / "wiki_results_by_asn.json"
    csv_out = output_dir / "wiki_results_by_asn.csv"

    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(asn_final_results, f, indent=2)
    print(f"\n📄 Saved JSON: {json_out}")

    with open(csv_out, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ASN", "Brand", "Classification_Result"])
        for asn, res in asn_final_results.items():
            clean = res["classification"].replace("\n", " ").strip()
            writer.writerow([asn, res["brand"], clean])
    print(f"📊 Saved CSV:  {csv_out}")


def build_wiki_tasks(
    wiki_json_path: Path,
    model: str,
    max_tokens: int,
    max_completion_tokens: int,
    temperature: float,
):
    total_query_brand = load_json_safe(wiki_json_path)
    task_lines = []

    if HAVE_PROMPT_TEMPLATE:
        base_template_len = count_tokens(
            f"{template_singlemodal_wiki}\n\n{taxonomy}\n\n{descr}", model
        )
    else:
        base_template_len = 50

    available_for_text = max(max_tokens - base_template_len - 100, 1000)
    print(
        f"Planning tasks: max prompt tokens={max_tokens}, "
        f"approx. space for wiki text={available_for_text}"
    )

    for i, (brand, info) in enumerate(total_query_brand.items(), start=1):
        raw_text = (info.get("full_text") or "").strip()
        if not raw_text:
            continue

        full_text = truncate_text(raw_text, available_for_text, model)

        if HAVE_PROMPT_TEMPLATE:
            base_prompt = (
                f"{template_singlemodal_wiki}\n\n"
                f"{taxonomy}\n\n"
                f"{descr}\n\n"
                f"Organization brand: {brand}\n\n"
                f"Wikipedia article full text:\n\n"
            )
        else:
            base_prompt = (
                f"Organization brand: {brand}\n\n"
                f"Below is the full text of its Wikipedia article.\n\n"
            )

        full_content = base_prompt + full_text
        est_tokens = count_tokens(full_content, model=model) + max_completion_tokens

        custom_id = f"wiki:{brand}"
        task = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model,
                "temperature": temperature,
                "max_completion_tokens": max_completion_tokens,
                "messages": [
                    {"role": "developer", "content": classification_wiki_instructions},
                    {"role": "user", "content": full_content},
                ],
            },
        }

        task_lines.append({"line": json.dumps(task, ensure_ascii=False), "tokens": est_tokens})

        if i % 1000 == 0:
            print(f"… processed {i} brands")

    print(f"\nGenerated {len(task_lines)} wiki classification tasks.")
    return task_lines


def run_fetch_only(args, client):
    jobs_path = Path(args.jobs_json)
    out_dir = Path(args.output_dir)
    if not jobs_path.is_file():
        sys.exit(f"❌ jobs-json not found: {jobs_path}")
    with open(jobs_path, encoding="utf-8") as f:
        jobs_map = json.load(f)
    if not isinstance(jobs_map, dict) or not jobs_map:
        sys.exit(f"❌ Invalid or empty jobs map: {jobs_path}")

    # Match post-submit behavior: mapping needs downloads, so wait if asn2brand given.
    paths = wait_and_download_batches(
        client,
        jobs_map,
        out_dir,
        wait=args.wait or bool(args.asn2brand_json),
        poll_interval=args.poll_interval,
    )
    if paths and args.asn2brand_json:
        parse_and_map_results(paths, Path(args.asn2brand_json), out_dir)
    elif not paths:
        print("No completed result files to parse.")
    elif not args.asn2brand_json:
        print("⚠️  Downloads done; pass --asn2brand-json to map ASNs.")


def main():
    parser = argparse.ArgumentParser(
        description="Wikipedia OpenAI batch: prepare, submit, wait, download, map to ASNs."
    )
    parser.add_argument(
        "--fetch-results-only",
        action="store_true",
        help="Only poll/download (and optionally map) using --jobs-json; skip building batch.",
    )
    parser.add_argument("--wiki-json", default=None, help="Path to wiki_info.json")
    parser.add_argument("--output-batch-jsonl", required=True, help="Path for generated batch JSONL.")
    parser.add_argument(
        "--jobs-json",
        default=None,
        help="wiki_batch_jobs.json (default: next to --output-batch-jsonl if omitted in fetch-only).",
    )
    parser.add_argument(
        "--asn2brand-json",
        default=None,
        help="classifiable_as2brand.json — required to write wiki_results_by_asn.*",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Raw batch downloads (result_*.jsonl) + mapped wiki_results_by_asn.*",
    )

    parser.add_argument("--model", default="gpt-5.2")
    parser.add_argument("--max-tokens", type=int, default=30000)
    parser.add_argument("--max-completion-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--tokens-per-batch-cap", type=int, default=DEFAULT_TOKENS_PER_BATCH_CAP)
    parser.add_argument(
        "--submit-batch",
        type=lambda x: str(x).lower() == "true",
        default=False,
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="After submit (or in fetch-only), poll until all jobs finish or fail.",
    )
    parser.add_argument("--poll-interval", type=int, default=60)

    args = parser.parse_args()

    if OpenAI is None and (args.submit_batch or args.fetch_results_only or args.wait):
        print("❌ OpenAI library not installed.")
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY")
    if args.fetch_results_only:
        if not api_key:
            print("❌ OPENAI_API_KEY not found.")
            sys.exit(1)
        batch_default = Path(args.output_batch_jsonl)
        jobs_path = Path(args.jobs_json) if args.jobs_json else wiki_jobs_log_path(batch_default)
        if args.jobs_json is None:
            args.jobs_json = str(jobs_path)
        if not Path(args.jobs_json).is_file():
            sys.exit(f"❌ jobs-json not found: {args.jobs_json}")
        client = OpenAI(api_key=api_key)
        run_fetch_only(args, client)
        return

    if not args.wiki_json:
        parser.error("--wiki-json is required unless --fetch-results-only")

    wiki_json_path = Path(args.wiki_json)
    batch_jsonl_path = Path(args.output_batch_jsonl)

    if not wiki_json_path.is_file():
        raise FileNotFoundError(f"wiki-json not found: {wiki_json_path}")

    tasks = build_wiki_tasks(
        wiki_json_path=wiki_json_path,
        model=args.model,
        max_tokens=args.max_tokens,
        max_completion_tokens=args.max_completion_tokens,
        temperature=args.temperature,
    )

    if not tasks:
        print("⚠️ No tasks generated.")
        return

    batch_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(batch_jsonl_path, "w", encoding="utf-8") as f:
        for item in tasks:
            f.write(item["line"] + "\n")
    print(f"✅ Full batch input saved to: {batch_jsonl_path}")

    chunks = plan_batch_chunks(tasks, args.tokens_per_batch_cap)
    base_stem = batch_jsonl_path.stem
    chunk_files = []

    print(f"\nPreparing {len(chunks)} chunk file(s)...")
    for i, chunk in enumerate(chunks, 1):
        part_suffix = f".part{i}.jsonl"
        chunk_path = batch_jsonl_path.with_name(f"{base_stem}{part_suffix}")
        if not chunk_path.exists():
            with open(chunk_path, "w", encoding="utf-8") as f:
                for line in chunk["tasks"]:
                    f.write(line + "\n")
        chunk_files.append(chunk_path)
        print(f"  [{i}] {chunk_path.name} (Reqs={len(chunk['tasks'])}, Tokens={chunk['est_tokens']})")

    if not args.submit_batch:
        print("\n🛑 Dry run complete. Use --submit-batch true to upload.")
        return

    if not api_key:
        print("❌ OPENAI_API_KEY not found.")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    jobs = {}
    job_log_path = wiki_jobs_log_path(batch_jsonl_path)

    print(f"\n🚀 Submitting {len(chunk_files)} chunk(s)...")
    for fpath in chunk_files:
        job_id = upload_and_run_batch(client, fpath)
        if job_id:
            jobs[fpath.name] = job_id
            persist_wiki_jobs(job_log_path, jobs)

    if jobs:
        persist_wiki_jobs(job_log_path, jobs)
        print(f"\n✅ Job IDs saved to: {job_log_path}")
    else:
        print("❌ No jobs submitted.")
        return

    out_dir = Path(args.output_dir)
    if args.wait or args.asn2brand_json:
        if not api_key:
            sys.exit(1)
        paths = wait_and_download_batches(
            client, jobs, out_dir, wait=args.wait or bool(args.asn2brand_json), poll_interval=args.poll_interval
        )
        if args.asn2brand_json and paths:
            parse_and_map_results(paths, Path(args.asn2brand_json), out_dir)
        elif args.asn2brand_json and not paths:
            print("⚠️  No downloaded results yet; run with --fetch-results-only when batches complete.")
    else:
        print("\nNext: when batches complete, run:")
        print(
            f"  python {Path(__file__).name} --fetch-results-only "
            f"--jobs-json {job_log_path} --asn2brand-json <classifiable_as2brand.json> "
            f"--output-dir {out_dir} --wait"
        )


if __name__ == "__main__":
    main()
