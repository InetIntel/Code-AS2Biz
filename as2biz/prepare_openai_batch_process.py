#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import argparse
import json
import os
import sys
import gzip
import re
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib

import tiktoken
import html2text
from warcio.archiveiterator import ArchiveIterator
from tqdm import tqdm

# === OpenAI Batch hard limits ===
MAX_REQUESTS_PER_BATCH = 50_000
MAX_BYTES_PER_BATCH = 190 * 1024 * 1024
DEFAULT_TOKENS_PER_BATCH_CAP = 180_000_000  # Tier 4 is 200M; leave headroom

# === dependency check ===
try:
    from dotenv import load_dotenv
    from openai import OpenAI
except ImportError:
    print("⚠️ Warning: 'openai' or 'python-dotenv' not installed.")
    OpenAI = None
    load_dotenv = lambda: None

try:
    from prompt import (
        template_singlemodal,
        taxonomy,
        descr,
        classification_instructions,
        taxonomy_list,
    )
    HAVE_PROMPT_TEMPLATE = True
except ImportError:
    raise
    HAVE_PROMPT_TEMPLATE = False

load_dotenv()

# =========================================================
# basic helpers
# =========================================================

SOCIAL_BASE_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "github.com",
    "google.com",
    "cloudflareaccess.com",
    "microsoftonline.com",
    "office.com",
    "live.com",
    "okta.com",
    "auth0.com",
}


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def append_jsonl(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def file_fingerprint(path: Path):
    if not path.exists():
        return None
    st = path.stat()
    return {
        "path": str(path.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def norm_url_global(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if "://" in u:
        u = u.split("://", 1)[1]
    return u.rstrip("/").lower()


def norm_seed_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if "://" in u:
        u = u.split("://", 1)[1]
    return u.rstrip("/").lower()


def get_host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""


def base_domain_from_host(host: str) -> str:
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def is_social_host(host: str) -> bool:
    return base_domain_from_host(host) in SOCIAL_BASE_DOMAINS


def normalize_asn_label(v):
    s = str(v).strip().upper()
    if s.startswith("AS"):
        s = s[2:]
    return s.strip()


def parse_captured_at(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def open_warc_stream(path: Path):
    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return open(path, "rb")


def html_to_text(html_bytes: bytes) -> str:
    if not html_bytes:
        return ""
    conv = html2text.HTML2Text()
    conv.ignore_links = True
    conv.ignore_images = True
    conv.body_width = 0
    try:
        return conv.handle(html_bytes.decode("utf-8", errors="replace")).strip()
    except Exception:
        return ""


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


def _part_index_from_chunk_filename(name: str, base_stem: str, mode: str):
    m = re.match(rf"^{re.escape(base_stem)}\.{re.escape(mode)}\.part(\d+)\.jsonl$", name)
    return int(m.group(1)) if m else None


def sorted_existing_mode_chunk_paths(batch_jsonl_path: Path, mode: str) -> list[Path]:
    base_stem = batch_jsonl_path.stem
    parent = batch_jsonl_path.parent
    found = []
    if not parent.is_dir():
        return []
    for p in parent.iterdir():
        if not p.is_file():
            continue
        idx = _part_index_from_chunk_filename(p.name, base_stem, mode)
        if idx is not None:
            found.append((idx, p))
    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


def chunk_stats_from_jsonl_lines(lines: list, pid_to_item: dict, args) -> tuple:
    prompt_ids = []
    est_tokens = 0
    est_bytes = 0
    for line in lines:
        est_bytes += len(line.encode("utf-8")) + 1
        try:
            obj = json.loads(line)
            pid = obj.get("custom_id")
            if pid:
                prompt_ids.append(pid)
            it = pid_to_item.get(pid) if pid else None
            if it:
                est_tokens += it["tokens"]
            else:
                est_tokens += args.max_completion_tokens
        except Exception:
            pass
    return prompt_ids, est_tokens, est_bytes


# =========================================================
# Prompt / build / state / result paths
# =========================================================

def build_manifest_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_build_manifest.json")


def prompt_state_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_prompt_state.json")


def submission_events_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_submission_events.jsonl")


def chunk_manifest_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_chunk_manifest.json")


def prompt_results_log_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_prompt_responses_log.jsonl")


def prompt_results_latest_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_prompt_responses_latest.json")


def download_state_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_download_state.json")


def as2biz_main_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_as2biz_main.json")


def as2biz_main_meta_path(batch_jsonl_path: Path):
    return batch_jsonl_path.with_name(f"{batch_jsonl_path.stem}_as2biz_main_meta.json")


# =========================================================
# Build signature
# =========================================================

def make_build_signature(args, as2web_json: Path, index_path: Path):
    payload = {
        "as2web_json": file_fingerprint(as2web_json),
        "index_jsonl": file_fingerprint(index_path),
        "params": {
            "archives_dir": str(Path(args.archives_dir).resolve()),
            "version_tag": args.version_tag,
            "model": args.model,
            "max_tokens": args.max_tokens,
            "max_completion_tokens": args.max_completion_tokens,
            "temperature": args.temperature,
            "skip_unchanged_sites": args.skip_unchanged_sites,
            "output_mode": args.output_mode,
            "target_url2asn": file_fingerprint(Path(args.target_url2asn)) if args.target_url2asn else None,
        }
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    sig = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return sig, payload


# =========================================================
# Prompt state / chunk state
# =========================================================

def load_prompt_state(path: Path):
    return safe_read_json(path, default={})


def save_prompt_state(path: Path, state: dict):
    safe_write_json(path, state)


def load_chunk_manifest(path: Path):
    return safe_read_json(path, default={})


def save_chunk_manifest(path: Path, data: dict):
    safe_write_json(path, data)


def ensure_prompt_state_entry(state: dict, prompt_id: str):
    if prompt_id not in state:
        state[prompt_id] = {
            "submitted": False,
            "completed": False,
            "latest_status": None,
            "jobs": [],
            "last_updated_at": utc_now_iso(),
        }


def mark_prompts_submitted(state: dict, prompt_ids, mode: str, chunk_name: str, job_id: str):
    ts = utc_now_iso()
    for pid in prompt_ids:
        ensure_prompt_state_entry(state, pid)
        state[pid]["submitted"] = True
        state[pid]["latest_status"] = "submitted"
        state[pid]["jobs"].append({
            "job_id": job_id,
            "mode": mode,
            "chunk": chunk_name,
            "submitted_at": ts,
        })
        state[pid]["last_updated_at"] = ts


def mark_prompts_status(state: dict, prompt_ids, status: str):
    ts = utc_now_iso()
    for pid in prompt_ids:
        ensure_prompt_state_entry(state, pid)
        state[pid]["latest_status"] = status
        if status == "completed":
            state[pid]["completed"] = True
        state[pid]["last_updated_at"] = ts


def collect_already_submitted_prompt_ids(state: dict):
    return {pid for pid, meta in state.items() if meta.get("submitted")}


# =========================================================
# Result extraction / freshness
# =========================================================

def flatten_message_content(content):
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
                if "text" in item and isinstance(item["text"], str):
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


def extract_response_text_from_batch_line(obj: dict) -> str:
    response = obj.get("response") or {}
    body = response.get("body") or {}

    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        return flatten_message_content(msg.get("content"))

    output = body.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        parts.append(c["text"])
        if parts:
            return "".join(parts).strip()

    return ""


def extract_valid_categories(response_text):
    if not response_text:
        return []

    matched_categories = []
    for category in taxonomy_list:
        if re.search(r'[^a-zA-Z0-9\s]', category):
            pattern = re.escape(category)
        else:
            pattern = r'\b' + re.escape(category) + r'\b'

        if re.search(pattern, response_text, re.IGNORECASE):
            matched_categories.append(category)

    return matched_categories


def _freshness_tuple(rec: dict):
    return (
        int(rec.get("completed_at_ts") or 0),
        str(rec.get("downloaded_at") or ""),
        str(rec.get("job_id") or ""),
    )


def _is_newer_record(new_rec: dict, old_rec: dict) -> bool:
    return _freshness_tuple(new_rec) > _freshness_tuple(old_rec)


def _read_file_content_text(file_content_obj) -> str:
    if hasattr(file_content_obj, "text"):
        txt = file_content_obj.text
        if isinstance(txt, str):
            return txt

    if hasattr(file_content_obj, "read"):
        data = file_content_obj.read()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)

    if hasattr(file_content_obj, "content"):
        data = file_content_obj.content
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        if isinstance(data, str):
            return data

    try:
        return str(file_content_obj)
    except Exception:
        return ""


def persist_job_log(batch_jsonl_path: Path, mode: str, jobs: dict) -> None:
    """Merge current session job ids into batch_jobs_<mode>.json (survives crash during status wait)."""
    job_log = batch_jsonl_path.parent / f"batch_jobs_{mode}.json"
    existing = safe_read_json(job_log, default={})
    if not isinstance(existing, dict):
        existing = {}
    existing.update(jobs)
    safe_write_json(job_log, existing)


def _job_ids_from_submission_events(events_path: Path) -> dict:
    """Recover chunk_file -> job_id from submit events (last occurrence wins)."""
    out = {}
    if not events_path.exists():
        return out
    try:
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                if obj.get("event") != "submit":
                    continue
                jid = obj.get("job_id")
                cf = obj.get("chunk_file")
                if isinstance(jid, str) and cf:
                    out[str(cf)] = jid
    except Exception as e:
        print(f"⚠️ Failed to read submission events {events_path}: {e}")
    return out


def _collect_all_job_ids(batch_jsonl_path: Path) -> dict:
    out = {}
    for mode in ["preview", "resume", "all"]:
        p = batch_jsonl_path.parent / f"batch_jobs_{mode}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    out.update(data)
            except Exception as e:
                print(f"⚠️ Failed to read {p}: {e}")
    ev_map = _job_ids_from_submission_events(submission_events_path(batch_jsonl_path))
    out.update(ev_map)
    return out


def download_and_materialize_results(
    client,
    batch_jsonl_path: Path,
    mapping_path: Path,
):
    results_log_p = prompt_results_log_path(batch_jsonl_path)
    results_latest_p = prompt_results_latest_path(batch_jsonl_path)
    download_state_p = download_state_path(batch_jsonl_path)
    as2biz_p = as2biz_main_path(batch_jsonl_path)
    as2biz_meta_p = as2biz_main_meta_path(batch_jsonl_path)

    final_mapping = safe_read_json(mapping_path, default={})
    latest_results = safe_read_json(results_latest_p, default={})
    dl_state = safe_read_json(download_state_p, default={"jobs": {}})

    chunk_to_job = _collect_all_job_ids(batch_jsonl_path)
    if not chunk_to_job:
        print("⚠️ No job IDs found (batch_jobs_*.json or *_submission_events.jsonl). Nothing to download.")
        return

    total_jobs = len(chunk_to_job)
    downloaded_jobs = 0
    updated_prompts = 0

    print(f"\n>>> Downloading completed batch outputs from {total_jobs} job(s)...")

    for chunk_name, job_id in chunk_to_job.items():
        try:
            batch = client.batches.retrieve(job_id)
        except Exception as e:
            print(f"⚠️ Failed to retrieve batch {job_id}: {e}")
            continue

        status = getattr(batch, "status", None)
        output_file_id = getattr(batch, "output_file_id", None)
        completed_at = getattr(batch, "completed_at", None)

        if status != "completed":
            print(f"  - {chunk_name}: status={status}, skip for now.")
            continue

        if not output_file_id:
            print(f"  - {chunk_name}: completed but no output_file_id, skip.")
            continue

        old_job_state = (dl_state.get("jobs") or {}).get(job_id, {})
        if (
            old_job_state.get("output_file_id") == output_file_id
            and int(old_job_state.get("completed_at_ts") or 0) == int(completed_at or 0)
        ):
            print(f"  - {chunk_name}: already downloaded this exact output_file_id, skip.")
            continue

        print(f"  - {chunk_name}: downloading output_file_id={output_file_id} ...")

        try:
            file_obj = client.files.content(output_file_id)
            text = _read_file_content_text(file_obj)
        except Exception as e:
            print(f"    ❌ Failed to download output file {output_file_id}: {e}")
            continue

        downloaded_at = utc_now_iso()
        line_count = 0
        prompt_updates_this_job = 0

        for raw_line in text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            line_count += 1
            try:
                obj = json.loads(raw_line)
            except Exception:
                continue

            prompt_id = obj.get("custom_id")
            if not prompt_id:
                continue

            response = obj.get("response") or {}
            status_code = response.get("status_code")
            error_obj = obj.get("error")
            response_text = extract_response_text_from_batch_line(obj)

            meta = final_mapping.get(prompt_id, {})
            mapped_asns = meta.get("asns", [])
            landing_urls = meta.get("landing_urls", [])
            included_urls = meta.get("included_urls", [])

            record = {
                "prompt_id": prompt_id,
                "job_id": job_id,
                "chunk_name": chunk_name,
                "output_file_id": output_file_id,
                "batch_status": status,
                "status_code": status_code,
                "completed_at_ts": int(completed_at or 0),
                "downloaded_at": downloaded_at,
                "response_text": response_text,
                "error": error_obj,
                "asns": mapped_asns,
                "landing_urls": landing_urls,
                "included_urls": included_urls,
            }

            append_jsonl(results_log_p, record)

            old = latest_results.get(prompt_id)
            if old is None or _is_newer_record(record, old):
                latest_results[prompt_id] = record
                updated_prompts += 1
                prompt_updates_this_job += 1

        dl_state.setdefault("jobs", {})[job_id] = {
            "chunk_name": chunk_name,
            "output_file_id": output_file_id,
            "completed_at_ts": int(completed_at or 0),
            "downloaded_at": downloaded_at,
            "status": status,
            "lines_seen": line_count,
            "prompt_updates": prompt_updates_this_job,
        }

        downloaded_jobs += 1
        print(f"    ✅ lines={line_count}, updated_prompts={prompt_updates_this_job}")

    safe_write_json(results_latest_p, latest_results)
    safe_write_json(download_state_p, dl_state)

    print(f"\n✅ Updated latest prompt results: {results_latest_p}")
    print(f"✅ Updated download state: {download_state_p}")
    print(f"   downloaded_jobs={downloaded_jobs}, updated_prompts={updated_prompts}")

    # ===== ASN-level as2biz_main =====
    asn_latest_meta = {}

    for prompt_id, rec in latest_results.items():
        response_text = rec.get("response_text") or ""
        cats = extract_valid_categories(response_text)

        asns = rec.get("asns") or final_mapping.get(prompt_id, {}).get("asns", [])
        if not asns:
            continue

        candidate = {
            "prompt_id": prompt_id,
            "categories": cats,
            "response_text": response_text,
            "job_id": rec.get("job_id"),
            "status_code": rec.get("status_code"),
            "completed_at_ts": int(rec.get("completed_at_ts") or 0),
            "downloaded_at": rec.get("downloaded_at"),
            "landing_urls": rec.get("landing_urls", []),
            "included_urls": rec.get("included_urls", []),
        }

        for asn in asns:
            asn = str(asn)
            old = asn_latest_meta.get(asn)
            if old is None or _freshness_tuple(candidate) > _freshness_tuple(old):
                asn_latest_meta[asn] = candidate

    as2biz_main = {asn: meta["categories"] for asn, meta in asn_latest_meta.items()}
    safe_write_json(as2biz_p, as2biz_main)
    safe_write_json(as2biz_meta_p, asn_latest_meta)

    print(f"✅ Saved ASN->categories main result: {as2biz_p}")
    print(f"✅ Saved ASN->categories meta result: {as2biz_meta_p}")
    print(f"   total_asns={len(as2biz_main)}")


# =========================================================
# WARC worker
# =========================================================

def worker_extract_warc(args):
    warc_path_str, target_ids_list = args
    warc_path = Path(warc_path_str)
    target_ids = set(target_ids_list)

    conv = html2text.HTML2Text()
    conv.ignore_links = True
    conv.ignore_images = True
    conv.body_width = 0

    result = {}
    try:
        with open_warc_stream(warc_path) as stream:
            for record in ArchiveIterator(stream):
                rec_id = record.rec_headers.get_header("WARC-Record-ID")
                if rec_id in target_ids:
                    try:
                        payload = record.content_stream().read()
                        text = conv.handle(payload.decode("utf-8", errors="replace")).strip()
                    except Exception:
                        text = ""
                    result[rec_id] = text
    except Exception as e:
        raise RuntimeError(f"Error reading {warc_path.name}: {e}") from e

    return result


# =========================================================
# ASN mapping
# =========================================================

def load_asn_mappings(as2web_json_path: Path):
    print(f"Loading AS2Web mapping from {as2web_json_path}...")
    try:
        data = json.loads(as2web_json_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load AS2Web JSON: {e}")
        sys.exit(1)

    url_to_asn = {}
    host_to_asn = {}
    seed_norm_to_asn = {}
    asn_to_expected = defaultdict(set)
    seed_norm_to_asns = defaultdict(set)

    def norm_url_for_seed(u: str) -> str:
        u = u.strip()
        if not u:
            return ""
        if "://" in u:
            u = u.split("://", 1)[1]
        return u.rstrip("/").lower()

    def add_entry(url_str, asn_val, *, is_seed=False):
        if not isinstance(url_str, str) or not url_str.strip():
            return

        raw = url_str.strip()
        raw_for_host = raw if "://" in raw else "http://" + raw

        norm_u = norm_url_global(raw)
        if norm_u:
            url_to_asn[norm_u] = asn_val

        host = get_host(raw_for_host)
        if host:
            base = base_domain_from_host(host)
            if base and not is_social_host(host):
                h = host.lower()
                if ":" in h:
                    h = h.split(":", 1)[0]
                if h:
                    host_to_asn[h] = asn_val
                    if h.startswith("www."):
                        host_to_asn[h[4:]] = asn_val

        if is_seed:
            seed_norm = norm_url_for_seed(url_str)
            if seed_norm:
                seed_norm_to_asn[seed_norm] = asn_val
                seed_norm_to_asns[seed_norm].add(asn_val)

    def extract_urls_from_content(content):
        urls = []
        if isinstance(content, dict):
            for key in ("Website", "URL", "website", "url", "homepage"):
                v = content.get(key)
                if isinstance(v, str) and v.strip():
                    urls.append(v.strip())
            for k in content.keys():
                if (
                    isinstance(k, str)
                    and "." in k
                    and k not in ["Website", "URL", "website", "url", "homepage", "Confidence", "Source"]
                ):
                    urls.append(k)
        elif isinstance(content, list):
            for v in content:
                if isinstance(v, str) and v.strip():
                    urls.append(v.strip())
        elif isinstance(content, str):
            urls.append(content.strip())
        return urls

    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    if isinstance(data, dict):
        for asn, content in data.items():
            asn_str = str(asn)
            urls = extract_urls_from_content(content)
            if not urls:
                continue
            first = True
            for u in urls:
                add_entry(u, asn_str, is_seed=first)
                first = False
            for u in urls:
                u2 = u if "://" in u else "http://" + u
                try:
                    h = urlparse(u2).netloc.lower()
                    if h.startswith("www."):
                        h = h[4:]
                    asn_to_expected[asn_str].add(h)
                except Exception:
                    pass

    elif isinstance(data, list):
        for rec in data:
            if not isinstance(rec, dict):
                continue
            asn_val = rec.get("asn") or rec.get("ASN") or rec.get("as")
            if asn_val is None:
                continue
            asn_str = str(asn_val)

            urls = extract_urls_from_content(rec)
            if not urls:
                continue

            first = True
            for u in urls:
                add_entry(u, asn_str, is_seed=first)
                first = False

            for u in urls:
                u2 = u if "://" in u else "http://" + u
                try:
                    h = urlparse(u2).netloc.lower()
                    if h.startswith("www."):
                        h = h[4:]
                    asn_to_expected[asn_str].add(h)
                except Exception:
                    pass
    else:
        print(f"⚠️ Unexpected AS2Web JSON top-level type: {type(data)}")

    print(f"✅ Mappings Loaded: {len(url_to_asn)} URLs, {len(host_to_asn)} Hosts.")
    return url_to_asn, host_to_asn, asn_to_expected, seed_norm_to_asn, seed_norm_to_asns


def find_asn(url, seed, site_field, url_map, host_map, seed_norm_to_asn):
    if seed:
        seed_norm = norm_seed_url(seed)
        if seed_norm in seed_norm_to_asn:
            return seed_norm_to_asn[seed_norm]

    norm_url = norm_url_global(url)
    if norm_url in url_map:
        return url_map[norm_url]

    host = get_host(url)
    if host in host_map:
        return host_map[host]
    h2 = host[4:] if host.startswith("www.") else host
    if h2 in host_map:
        return host_map[h2]

    if seed:
        seed_host = get_host(seed)
        if not is_social_host(seed_host):
            if seed_host in host_map:
                return host_map[seed_host]
            sh2 = seed_host[4:] if seed_host.startswith("www.") else seed_host
            if sh2 in host_map:
                return host_map[sh2]

    if site_field:
        s = site_field.strip().lower()
        if s in host_map:
            return host_map[s]

    return "Unknown_ASN"


# =========================================================
# OpenAI Batch
# =========================================================

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


# =========================================================
# main flow
# =========================================================

def main():
    parser = argparse.ArgumentParser(
        description="ASN-level Batch Prep with build manifest + unified prompt submission state + result downloading."
    )
    parser.add_argument("--as2web-json", required=True)
    parser.add_argument("--archives-dir", required=True)
    parser.add_argument("--version-tag", required=True)
    parser.add_argument(
        "--output-batch-jsonl",
        default=None,
        help="Default: ./tmp/<version-tag>/batch_input.jsonl",
    )
    parser.add_argument(
        "--output-mapping-json",
        default=None,
        help="Default: ./tmp/<version-tag>/batch_mapping.json (then renamed next to batch jsonl)",
    )

    parser.add_argument("--model", default="gpt-5.2")
    parser.add_argument("--max-tokens", type=int, default=25000)
    parser.add_argument("--max-completion-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--tokens-per-batch-cap", type=int, default=DEFAULT_TOKENS_PER_BATCH_CAP)

    parser.add_argument("--output-mode", choices=["openai", "local"], default="openai")
    parser.add_argument(
        "--output-local-prompts",
        default=None,
        help="Default: ./tmp/<version-tag>/local_prompts.jsonl.gz",
    )
    parser.add_argument("--local-limit", type=int, default=0)

    parser.add_argument("--mode", choices=["preview", "resume", "all"], default="preview")
    parser.add_argument("--preview-size", type=int, default=1000)
    parser.add_argument("--submit-batch", type=lambda x: str(x).lower() == "true", default=False)
    parser.add_argument("--force-resubmit", type=lambda x: str(x).lower() == "true", default=False)
    parser.add_argument(
        "--repack-chunks",
        type=lambda x: str(x).lower() == "true",
        default=False,
        help="RESUME only: ignore existing *.part*.jsonl on disk and re-chunk from scratch (default: reuse part files).",
    )
    parser.add_argument(
        "--skip-unchanged-sites",
        type=lambda x: str(x).lower() == "true",
        default=True,
        help="Skip ASNs whose winner site only has reused_from_store pages.",
    )
    parser.add_argument(
        "--skip-chunks",
        default="",
        help="Comma-separated chunk filenames to skip when submitting.",
    )
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--preview-targets", help="Path to text/JSON file with target ASNs.")
    parser.add_argument(
        "--target-url2asn",
        default=None,
        help="Optional JSON file mapping {url: [asn,...]}. Only target ASNs will be processed.",
    )
    parser.add_argument(
        "--download-results",
        type=lambda x: str(x).lower() == "true",
        default=False,
        help="Download completed batch outputs and materialize prompt/asn results."
    )
    parser.add_argument(
        "--download-results-only",
        type=lambda x: str(x).lower() == "true",
        default=False,
        help="Skip submit logic; only download completed outputs and refresh local result files."
    )

    args = parser.parse_args()

    if args.output_mode == "local":
        args.mode = "all"
        args.submit_batch = False

    tmp_version_dir = Path("./tmp") / args.version_tag
    mapping_used_default = args.output_mapping_json is None
    if args.output_batch_jsonl is None:
        args.output_batch_jsonl = str(tmp_version_dir / "batch_input.jsonl")
    if args.output_mapping_json is None:
        args.output_mapping_json = str(tmp_version_dir / "batch_mapping.json")
    if args.output_local_prompts is None:
        args.output_local_prompts = str(tmp_version_dir / "local_prompts.jsonl.gz")

    batch_jsonl_path = Path(args.output_batch_jsonl)
    mapping_path = Path(args.output_mapping_json)
    if mapping_used_default:
        stem = batch_jsonl_path.stem
        mapping_path = batch_jsonl_path.with_name(f"{stem}_mapping.json")
        print(f"Auto-renamed mapping file to match batch: {mapping_path}")

    manifest_path = build_manifest_path(batch_jsonl_path)
    state_path = prompt_state_path(batch_jsonl_path)
    events_path = submission_events_path(batch_jsonl_path)
    cmanifest_path = chunk_manifest_path(batch_jsonl_path)

    if args.download_results_only:
        if OpenAI is None:
            print("❌ OpenAI library not installed.")
            sys.exit(1)

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("❌ OPENAI_API_KEY not found.")
            sys.exit(1)

        if not mapping_path.exists():
            print(f"❌ Mapping file not found: {mapping_path}")
            sys.exit(1)

        client = OpenAI(api_key=api_key)
        download_and_materialize_results(client, batch_jsonl_path, mapping_path)
        sys.exit(0)

    prompt_state = load_prompt_state(state_path)
    chunk_manifest = load_chunk_manifest(cmanifest_path)

    version_dir = Path(args.archives_dir) / "versions" / args.version_tag
    index_path = version_dir / "index.jsonl"
    as2web_path = Path(args.as2web_json)

    build_sig, build_payload = make_build_signature(args, as2web_path, index_path)
    existing_manifest = safe_read_json(manifest_path, default=None)

    all_task_lines = []
    final_mapping = {}
    reuse_existing = False

    # =========================================================
    # Phase 0: build-level cache check
    # =========================================================
    if (
        args.mode == "preview"
        and batch_jsonl_path.exists()
        and mapping_path.exists()
        and existing_manifest
        and existing_manifest.get("build_signature") == build_sig
    ):
        print("\n>>> MODE: PREVIEW (reusing existing batch because build manifest matches)")
        reuse_existing = True

        est_tokens_per_task = args.max_tokens + args.max_completion_tokens
        with open(batch_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    pid = obj.get("custom_id")
                    all_task_lines.append({
                        "line": line,
                        "tokens": est_tokens_per_task,
                        "prompt_id": pid,
                    })
                except Exception:
                    continue

        final_mapping = safe_read_json(mapping_path, default={})
        print(f"Loaded {len(all_task_lines)} tasks from existing batch JSONL.")

    # =========================================================
    # Phase 1: generate prompts (preview/all, build cache miss)
    # =========================================================
    if args.mode in ["preview", "all"] and not reuse_existing:
        print(f"\n>>> MODE: {args.mode.upper()} (Scan WARCs, group by ASN, build prompts)")

        url_to_asn, host_to_asn, asn_to_expected, seed_norm_to_asn, seed_norm_to_asns = load_asn_mappings(as2web_path)

        print(f"Reading Slice Index: {index_path} ...")
        pages_by_key = {}

        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue

                url = row.get("url", "")
                seed = row.get("seed", "")
                site_field = row.get("site", "")

                url_host = get_host(url)
                seed_host = get_host(seed) if seed else ""
                if seed_host and (not is_social_host(seed_host)) and is_social_host(url_host):
                    continue

                store_ref = row.get("store_ref") or {}
                rid = store_ref.get("record_id")
                warc = store_ref.get("warc")
                if not rid or not warc:
                    continue

                asn = find_asn(url, seed, site_field, url_to_asn, host_to_asn, seed_norm_to_asn)
                norm_url = norm_url_global(url)
                norm_seed = norm_seed_url(seed) if isinstance(seed, str) else ""
                captured_at = row.get("captured_at", "")
                ts = parse_captured_at(captured_at)

                key = (asn, norm_url)
                entry = {
                    "asn": asn,
                    "url": url,
                    "norm_url": norm_url,
                    "seed": seed,
                    "norm_seed": norm_seed,
                    "site": site_field,
                    "length": row.get("length", 0),
                    "store_warc": warc,
                    "record_id": rid,
                    "captured_at": captured_at,
                    "captured_ts": ts,
                    "store_origin": row.get("store_origin"),
                }

                existing = pages_by_key.get(key)
                if existing is None:
                    pages_by_key[key] = entry
                else:
                    old_ts = existing["captured_ts"]
                    if old_ts is None and ts is not None:
                        pages_by_key[key] = entry
                    elif old_ts is not None and ts is not None and ts > old_ts:
                        pages_by_key[key] = entry

        print(f"Unique (ASN, URL) pages (after latest-only): {len(pages_by_key)}")

        raw_pages_by_asn = defaultdict(list)
        for entry in pages_by_key.values():
            raw_pages_by_asn[entry["asn"]].append(entry)

        print(f"Total ASNs before filtering: {len(raw_pages_by_asn)}")
        print(">>> Filtering: Winner-takes-all by site...")

        final_pages_by_asn = {}
        conflict_stats = []

        for asn, entries in raw_pages_by_asn.items():
            if not entries:
                continue
            if asn == "Unknown_ASN":
                continue

            site_groups = defaultdict(list)
            for e in entries:
                s = e.get("site")
                if not s:
                    try:
                        s = urlparse(e["url"]).netloc.lower()
                    except Exception:
                        s = "unknown"
                site_groups[s].append(e)

            ranked_sites = []
            expected_hosts = asn_to_expected.get(str(asn), set())

            for s, group in site_groups.items():
                is_expected = 1 if s in expected_hosts else 0
                page_count = len(group)
                total_bytes = sum(int(p.get("length", 0) or 0) for p in group)
                ranked_sites.append({
                    "site": s,
                    "entries": group,
                    "score": (is_expected, page_count, total_bytes),
                })

            # explicit tie-break for stability
            ranked_sites.sort(
                key=lambda x: (
                    x["score"][0],
                    x["score"][1],
                    x["score"][2],
                    x["site"],
                ),
                reverse=True
            )
            winner = ranked_sites[0]

            if len(site_groups) > 1:
                conflict_stats.append({
                    "asn": asn,
                    "site_count": len(site_groups),
                    "winner": winner["site"],
                    "candidates": [r["site"] for r in ranked_sites[:5]],
                })

            final_pages_by_asn[asn] = winner["entries"]

        conflict_stats.sort(key=lambda x: x["site_count"], reverse=True)
        if not conflict_stats:
            print("No ASNs mapped to multiple sites.")
        else:
            for i, item in enumerate(conflict_stats[:5], 1):
                if item["asn"] in final_pages_by_asn:
                    print(f"[{i}] ASN {item['asn']} (Mapped to {item['site_count']} sites)")
                    print(f"    WINNER: {item['winner']}")
                    print(f"    OTHERS: {item['candidates'][1:]} ...")
                    print("-" * 40)

        # target-url2asn allowlist filter
        if args.target_url2asn:
            tgt_path = Path(args.target_url2asn)
            if not tgt_path.exists():
                print(f"⚠️ Target file not found: {tgt_path}")
                sys.exit(1)

            print("\n" + "=" * 60)
            print(f"🎯 TARGET FILTER ACTIVE: Loading {tgt_path.name}...")

            try:
                with open(tgt_path, "r", encoding="utf-8") as f:
                    target_map = json.load(f)

                target_asns, target_as2url = set(), {}
                for url in target_map:
                    for asn in target_map[url]:
                        asn = normalize_asn_label(asn)
                        target_asns.add(asn)
                        target_as2url[asn] = url

                filtered_final = {}
                for asn, entries in final_pages_by_asn.items():
                    asn_norm = normalize_asn_label(asn)
                    if asn_norm in target_asns:
                        filtered_final[asn_norm] = entries

                covered_urls = set()
                for asn in filtered_final:
                    covered_urls.add(target_as2url[asn])

                covered_urls_count = len(covered_urls)
                print(f"   [Result] ASNs with Prompts: {len(filtered_final)} / {len(target_asns)} targets.")
                print(f"   [Result] Input URLs Covered: {covered_urls_count} / {len(target_map)} "
                      f"({covered_urls_count / len(target_map) * 100:.1f}%)")
                print("=" * 60 + "\n")

                final_pages_by_asn = filtered_final

            except Exception as e:
                print(f"❌ Error loading target-url2asn: {e}")
                sys.exit(1)

        # skip unchanged winner sites
        if args.skip_unchanged_sites:
            filtered_final2 = {}
            skipped_asns = []
            for asn, entries in final_pages_by_asn.items():
                changed = any(e.get("store_origin") != "reused_from_store" for e in entries)
                if changed:
                    filtered_final2[asn] = entries
                else:
                    skipped_asns.append(asn)

            final_pages_by_asn = filtered_final2
            print(
                f"Skip {len(skipped_asns)} ASN(s) whose winner site has only reused_from_store pages; "
                f"{len(final_pages_by_asn)} ASN(s) remain."
            )

        pages_by_asn = final_pages_by_asn
        print(f"Total ASNs after filtering: {len(pages_by_asn)}")

        warc_to_rec_ids = defaultdict(set)
        total_urls_final = 0
        for _asn, entries in pages_by_asn.items():
            total_urls_final += len(entries)
            for entry in entries:
                warc_to_rec_ids[entry["store_warc"]].add(entry["record_id"])

        print(f"Total Unique URLs to process: {total_urls_final}")

        print("\n[DEBUG] Post-Filtering Verification:")
        top_asns = sorted(pages_by_asn.items(), key=lambda x: len(x[1]), reverse=True)[:15]
        print(f"{'ASN':<15} | {'Total URLs':<12} | {'Unique Sites':<12} | {'Winner Site'}")
        print("-" * 60)
        for asn, pages in top_asns:
            unique_sites = set(p.get("site", "unknown") for p in pages)
            winner = list(unique_sites)[0] if unique_sites else "unknown"
            print(f"{asn:<15} | {len(pages):<12} | {len(unique_sites):<12} | {winner}")
        unknown_pages = pages_by_asn.get("Unknown_ASN", [])
        u_sites = set(p.get("site", "unknown") for p in unknown_pages)
        print(f"\n[DEBUG] Unknown_ASN: {len(unknown_pages)} URLs belonging to {len(u_sites)} unique sites.")

        store_warc_base = Path(args.archives_dir) / "store" / "warc"
        rec_text = {}

        print(">>> Extracting HTML text from store WARCs (parallel)...")
        tasks = []
        for warc_name, target_rids in warc_to_rec_ids.items():
            warc_path = store_warc_base / warc_name
            if warc_path.exists():
                tasks.append((str(warc_path), list(target_rids)))
            else:
                print(f"⚠️ Missing store WARC: {warc_name}")

        if tasks:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                future_to_task = {executor.submit(worker_extract_warc, t): t[0] for t in tasks}
                for future in tqdm(as_completed(future_to_task), total=len(future_to_task), unit="file", desc="WARCs"):
                    warc_path_str = future_to_task[future]
                    try:
                        partial = future.result()
                        rec_text.update(partial)
                    except Exception as e:
                        tqdm.write(f"❌ Error in worker for {warc_path_str}: {e}")
        else:
            print("⚠️ No WARC tasks to process. rec_text will be empty.")

        print(">>> Building per-ASN prompts (landing first, then subpages, with dedup)...")

        prompt_hash_to_task = {}
        prompt_id_to_meta = {}

        for asn, pages in pages_by_asn.items():
            for p in pages:
                p["is_landing"] = (p["norm_url"] and p["norm_url"] == p["norm_seed"])

            landing_candidates = [p for p in pages if p["is_landing"]]
            if landing_candidates:
                landing = max(
                    landing_candidates,
                    key=lambda e: (
                        e["captured_ts"] or datetime.min,
                        e["norm_url"] or "",
                        e["record_id"] or "",
                    )
                )
            else:
                landing = max(
                    pages,
                    key=lambda e: (
                        e["captured_ts"] or datetime.min,
                        e["norm_url"] or "",
                        e["record_id"] or "",
                    )
                )
                landing["is_landing"] = True

            subpages = [p for p in pages if p is not landing]
            subpages.sort(
                key=lambda e: (
                    e["captured_ts"] or datetime.min,
                    e["norm_url"] or "",
                    e["record_id"] or "",
                ),
                reverse=True
            )

            if landing["record_id"] not in rec_text:
                usable = [p for p in subpages if p["record_id"] in rec_text]
                if not usable:
                    continue
                landing = usable[0]
                subpages = usable[1:]

            rep_url = landing["url"]
            if HAVE_PROMPT_TEMPLATE:
                base_prompt = (
                    f"{template_singlemodal}\n\n{taxonomy}\n\n{descr}\n\n"
                    f"Site Text (landing page first, followed by subpages):\n\n"
                )
            else:
                raise RuntimeError("HAVE_PROMPT_TEMPLATE=False but non-template mode is not implemented")

            base_tokens = count_tokens(base_prompt, args.model)
            avail = max(args.max_tokens - base_tokens, 500)

            body_parts = []
            consumed = 0
            included_record_ids = []
            included_urls = []

            # This closure is called synchronously before the enclosing loop advances.
            def add_page(label, page_entry):
                nonlocal consumed
                rec_id = page_entry["record_id"]
                if rec_id not in rec_text:
                    return
                url = page_entry["url"]
                hdr = f"\n\n### {label}\n\n"
                text = rec_text.get(rec_id, "")

                hdr_tokens = count_tokens(hdr, args.model)
                if consumed + hdr_tokens >= avail:  # noqa: B023
                    return

                remaining = avail - consumed - hdr_tokens  # noqa: B023
                if remaining <= 0:
                    return

                body_tokens = count_tokens(text, args.model)
                if body_tokens <= remaining:
                    body = text
                    used_tokens = hdr_tokens + body_tokens
                else:
                    body = truncate_text(text, remaining, args.model)
                    used_tokens = hdr_tokens + remaining

                body_parts.append(hdr + body)  # noqa: B023
                consumed += used_tokens
                included_record_ids.append(rec_id)  # noqa: B023
                included_urls.append(url)  # noqa: B023

            add_page("Landing", landing)
            for sp in subpages:
                if consumed >= avail:
                    break
                add_page("Subpage", sp)

            aggregated_body = "".join(body_parts)
            full_content = base_prompt + aggregated_body

            prompt_hash = hashlib.sha256(full_content.encode("utf-8")).hexdigest()
            build_id = build_sig[:12]
            prompt_id = f"{build_id}:prompt:{prompt_hash[:16]}"

            est_tokens = count_tokens(full_content, args.model) + args.max_completion_tokens

            if prompt_hash not in prompt_hash_to_task:
                task = {
                    "custom_id": prompt_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": args.model,
                        "temperature": args.temperature,
                        "max_completion_tokens": args.max_completion_tokens,
                        "messages": [
                            {"role": "developer", "content": classification_instructions},
                            {"role": "user", "content": full_content},
                        ],
                    },
                }

                prompt_hash_to_task[prompt_hash] = {
                    "line": json.dumps(task, ensure_ascii=False),
                    "tokens": est_tokens,
                    "prompt_id": prompt_id,
                    "prompt_hash": prompt_hash,
                }

                prompt_id_to_meta[prompt_id] = {
                    "prompt_hash": prompt_hash,
                    "build_id": build_id,
                    "model": args.model,
                    "max_tokens": args.max_tokens,
                    "max_completion_tokens": args.max_completion_tokens,
                    "asns": set(),
                    "landing_urls": [],
                    "included_urls": [],
                    "included_record_ids": [],
                    "created_at": utc_now_iso(),
                }

            meta = prompt_id_to_meta[prompt_id]
            seed_norm = landing.get("norm_seed", "") or ""
            asn_set = seed_norm_to_asns.get(seed_norm, set()) or {asn}
            meta["asns"].update(asn_set)
            meta["landing_urls"].append(rep_url)
            meta["included_urls"].extend(included_urls)
            meta["included_record_ids"].extend(included_record_ids)

        all_task_lines = list(prompt_hash_to_task.values())
        final_mapping = {pid: meta for pid, meta in prompt_id_to_meta.items()}

        for meta in final_mapping.values():
            if isinstance(meta.get("asns"), set):
                meta["asns"] = sorted(meta["asns"], key=str)

        print(f"\nGenerated {len(all_task_lines)} UNIQUE prompts "
              f"for {sum(len(m['asns']) for m in final_mapping.values())} ASNs.")

        if args.output_mode == "local":
            local_path = Path(args.output_local_prompts)
            local_path.parent.mkdir(parents=True, exist_ok=True)

            def _min_asn_key(item):
                pid = item["prompt_id"]
                asns = final_mapping.get(pid, {}).get("asns", [])
                nums = []
                for a in asns:
                    try:
                        nums.append(int(a))
                    except Exception:
                        pass
                return (min(nums) if nums else float("inf"), pid)

            sorted_items = sorted(all_task_lines, key=_min_asn_key)
            limit = args.local_limit if args.local_limit > 0 else len(sorted_items)

            count = 0
            with gzip.open(local_path, "wt", encoding="utf-8") as f:
                for task_info in sorted_items[:limit]:
                    prompt_id = task_info["prompt_id"]
                    meta = final_mapping.get(prompt_id, {})
                    openai_task = json.loads(task_info["line"])
                    messages = openai_task["body"]["messages"]
                    row = {
                        "prompt_id": prompt_id,
                        "asns": meta.get("asns", []),
                        "landing_urls": meta.get("landing_urls", []),
                        "included_urls": meta.get("included_urls", []),
                        "system_prompt": messages[0]["content"],
                        "user_prompt": messages[1]["content"],
                    }
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    count += 1

            size_mb = local_path.stat().st_size / (1024 * 1024)
            print(f"\n✅ Local prompts saved to: {local_path} ({count}/{len(sorted_items)} prompts, {size_mb:.1f} MB compressed)")
            print("Done.")
            sys.exit(0)

        print(f"\nGenerated {len(all_task_lines)} ASN-level tasks.")

        batch_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(batch_jsonl_path, "w", encoding="utf-8") as f:
            for item in all_task_lines:
                f.write(item["line"] + "\n")
        print(f"✅ Full Batch Input saved to: {batch_jsonl_path}")

        safe_write_json(mapping_path, final_mapping)
        print(f"✅ ASN Mapping saved to: {mapping_path}")

        manifest = {
            "build_signature": build_sig,
            "build_id": build_sig[:12],
            "created_at": utc_now_iso(),
            "build_payload": build_payload,
            "batch_file": str(batch_jsonl_path),
            "mapping_file": str(mapping_path),
            "prompt_count": len(all_task_lines),
            "asn_count": sum(len(m.get("asns", [])) for m in final_mapping.values()),
        }
        safe_write_json(manifest_path, manifest)
        print(f"✅ Build manifest saved to: {manifest_path}")

    # =========================================================
    # Phase 2: resume, load from disk
    # =========================================================
    elif args.mode == "resume":
        print(f"\n>>> MODE: RESUME (Loading from {batch_jsonl_path})")
        if not batch_jsonl_path.exists():
            print(f"❌ Error: {batch_jsonl_path} not found. Run preview/all first.")
            sys.exit(1)

        final_mapping = safe_read_json(mapping_path, default={})
        with open(batch_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    pid = obj.get("custom_id")
                    msgs = obj["body"]["messages"]
                    char_len = 0
                    for m in msgs:
                        c = m.get("content", "")
                        char_len += len(c) if isinstance(c, str) else len(str(c))
                    est_toks = int(char_len / 2.5) + args.max_completion_tokens
                    all_task_lines.append({
                        "line": line,
                        "tokens": est_toks,
                        "prompt_id": pid,
                    })
                except Exception:
                    continue

        print(f"Loaded {len(all_task_lines)} tasks from disk.")
        if args.preview_targets:
            print("⚠️ Note: --preview-targets is ignored in RESUME mode.")

    # =========================================================
    # Phase 3: select tasks to submit
    # =========================================================
    submitted_prompt_ids = collect_already_submitted_prompt_ids(prompt_state) if not args.force_resubmit else set()
    tasks_to_submit = []

    if args.mode == "preview":
        if args.preview_targets:
            target_file = Path(args.preview_targets)
            if not target_file.exists():
                sys.exit(f"❌ Target file not found: {target_file}")

            raw = target_file.read_text(encoding="utf-8").strip()
            targets = set()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    for v in parsed:
                        if v is not None:
                            targets.add(str(v).strip())
                else:
                    for line in raw.splitlines():
                        s = line.strip()
                        if s:
                            targets.add(s)
            except json.JSONDecodeError:
                for line in raw.splitlines():
                    s = line.strip()
                    if s:
                        targets.add(s)

            print(f">>> Targeted Preview: loaded {len(targets)} target ASNs from {target_file}")
            matched_asns = set()

            for item in all_task_lines:
                pid = item.get("prompt_id")
                if (not args.force_resubmit) and pid in submitted_prompt_ids:
                    continue
                if pid in final_mapping:
                    associated_asns = final_mapping[pid].get("asns", [])
                    hits = [str(a) for a in associated_asns if str(a).strip() in targets]
                    if hits:
                        tasks_to_submit.append(item)
                        matched_asns.update(hits)

            print(f"✅ Target summary: {len(targets)} target ASNs in file; "
                  f"{len(matched_asns)} of them have at least one prompt; "
                  f"{len(tasks_to_submit)} prompts selected.")
        else:
            unsubmitted = []
            for item in all_task_lines:
                pid = item.get("prompt_id")
                if (not args.force_resubmit) and pid in submitted_prompt_ids:
                    continue
                unsubmitted.append(item)

            limit = min(len(unsubmitted), args.preview_size)
            tasks_to_submit = unsubmitted[:limit]
            print(f"\n>>> PREVIEW MODE: Selecting first {limit} unsubmitted tasks.")

    elif args.mode == "resume":
        for item in all_task_lines:
            pid = item.get("prompt_id")
            if (not args.force_resubmit) and pid in submitted_prompt_ids:
                continue
            tasks_to_submit.append(item)
        print(f"\n>>> RESUME MODE: {len(tasks_to_submit)} unsubmitted tasks remain.")

    else:  # all
        for item in all_task_lines:
            pid = item.get("prompt_id")
            if (not args.force_resubmit) and pid in submitted_prompt_ids:
                continue
            tasks_to_submit.append(item)
        print(f"\n>>> ALL MODE: {len(tasks_to_submit)} tasks selected after submission-state filtering.")

    if not tasks_to_submit:
        print("No tasks selected.")
        if args.download_results:
            if OpenAI is None:
                print("❌ OpenAI library not installed, cannot download results.")
                sys.exit(1)
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                print("❌ OPENAI_API_KEY not found, cannot download results.")
                sys.exit(1)
            client = OpenAI(api_key=api_key)
            download_and_materialize_results(client, batch_jsonl_path, mapping_path)
        sys.exit(0)

    # =========================================================
    # Phase 4: chunk + write chunk + chunk manifest
    # =========================================================
    base_stem = batch_jsonl_path.stem
    chunk_files = []

    if args.mode == "resume" and not args.force_resubmit and not args.repack_chunks:
        # reuse existing *.resume.partN.jsonl on disk: skip files fully submitted;
        # re-plan the remaining pending tasks, writing new files from max(part)+1.
        pid_to_item = {}
        for item in all_task_lines:
            pid = item.get("prompt_id")
            if pid:
                pid_to_item[pid] = item

        part_re = re.compile(rf"^{re.escape(base_stem)}\.{re.escape(args.mode)}\.part\d+\.jsonl$")
        chunk_manifest = {k: v for k, v in chunk_manifest.items() if not part_re.match(k)}

        existing_paths = sorted_existing_mode_chunk_paths(batch_jsonl_path, args.mode)
        reuse_paths = []
        consumed_pids = set()

        print(f"\n>>> RESUME: scanning {len(existing_paths)} existing chunk file(s) under {batch_jsonl_path.parent} ...")

        for path in existing_paths:
            lines = []
            pids = []
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    s = raw.strip()
                    if not s:
                        continue
                    lines.append(s)
                    try:
                        pids.append(json.loads(s).get("custom_id"))
                    except Exception:
                        pids.append(None)
            pids = [p for p in pids if p]
            if not pids:
                print(f"  [skip] {path.name}: empty or unreadable.")
                continue

            n_sub = sum(1 for p in pids if p in submitted_prompt_ids)
            if n_sub == len(pids):
                print(f"  [skip] {path.name}: all {len(pids)} prompt(s) already submitted.")
                continue
            if n_sub > 0:
                print(
                    f"  ⚠️ {path.name}: mixed submitted/unsubmitted ({n_sub}/{len(pids)}), "
                    f"not reusing; those tasks will be included in repack if still pending."
                )
                continue

            reuse_paths.append(path)
            consumed_pids.update(pids)

        orphan_items = [it for it in tasks_to_submit if it.get("prompt_id") not in consumed_pids]

        for path in reuse_paths:
            lines = []
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    s = raw.strip()
                    if s:
                        lines.append(s)
            prompt_ids, etok, ebytes = chunk_stats_from_jsonl_lines(lines, pid_to_item, args)
            chunk_manifest[path.name] = {
                "mode": args.mode,
                "prompt_ids": prompt_ids,
                "est_tokens": etok,
                "est_bytes": ebytes,
                "task_count": len(lines),
                "updated_at": utc_now_iso(),
                "reused_from_disk": True,
            }
            chunk_files.append(path)
            print(
                f"  [reuse] {path.name} (tasks={len(lines)}, est_tokens~={etok}, "
                f"{ebytes/(1024*1024):.1f}MB)"
            )

        max_part = 0
        for path in existing_paths:
            idx = _part_index_from_chunk_filename(path.name, base_stem, args.mode)
            if idx is not None:
                max_part = max(max_part, idx)

        if orphan_items:
            print(f"\n>>> Repacking {len(orphan_items)} task(s) not covered by reused chunks → new part file(s)...")
            chunks_new = plan_batch_chunks(orphan_items, args.tokens_per_batch_cap)
            for j, chunk in enumerate(chunks_new):
                i = max_part + 1 + j
                part_suffix = f".{args.mode}.part{i}.jsonl"
                chunk_path = batch_jsonl_path.with_name(f"{base_stem}{part_suffix}")

                prompt_ids = []
                for line in chunk["tasks"]:
                    try:
                        prompt_ids.append(json.loads(line)["custom_id"])
                    except Exception:
                        pass

                with open(chunk_path, "w", encoding="utf-8") as f:
                    for line in chunk["tasks"]:
                        f.write(line + "\n")
                print(
                    f"  [{j+1}/{len(chunks_new)}] Saved {chunk_path.name} "
                    f"(Reqs={len(chunk['tasks'])}, Tokens={chunk['est_tokens']}, "
                    f"Size={chunk['est_bytes']/(1024*1024):.1f}MB)"
                )

                chunk_manifest[chunk_path.name] = {
                    "mode": args.mode,
                    "prompt_ids": prompt_ids,
                    "est_tokens": chunk["est_tokens"],
                    "est_bytes": chunk["est_bytes"],
                    "task_count": len(chunk["tasks"]),
                    "updated_at": utc_now_iso(),
                }
                chunk_files.append(chunk_path)
        else:
            print(f"\n>>> All pending tasks are covered by reused chunk files ({len(reuse_paths)} file(s)).")

    else:
        if args.mode == "resume" and args.repack_chunks:
            print("\n>>> RESUME + --repack-chunks true: ignoring on-disk part files, full re-chunk.")
        chunks = plan_batch_chunks(tasks_to_submit, args.tokens_per_batch_cap)

        print(f"\nPreparing {len(chunks)} chunk file(s)...")
        for i, chunk in enumerate(chunks, 1):
            part_suffix = f".{args.mode}.part{i}.jsonl"
            chunk_path = batch_jsonl_path.with_name(f"{base_stem}{part_suffix}")

            prompt_ids = []
            for line in chunk["tasks"]:
                try:
                    prompt_ids.append(json.loads(line)["custom_id"])
                except Exception:
                    pass

            if chunk_path.exists():
                print(f"  [{i}/{len(chunks)}] {chunk_path.name} already exists, reusing existing file.")
            else:
                with open(chunk_path, "w", encoding="utf-8") as f:
                    for line in chunk["tasks"]:
                        f.write(line + "\n")
                print(f"  [{i}/{len(chunks)}] Saved {chunk_path.name} "
                      f"(Reqs={len(chunk['tasks'])}, Tokens={chunk['est_tokens']}, "
                      f"Size={chunk['est_bytes']/(1024*1024):.1f}MB)")

            chunk_manifest[chunk_path.name] = {
                "mode": args.mode,
                "prompt_ids": prompt_ids,
                "est_tokens": chunk["est_tokens"],
                "est_bytes": chunk["est_bytes"],
                "task_count": len(chunk["tasks"]),
                "updated_at": utc_now_iso(),
            }
            chunk_files.append(chunk_path)

    save_chunk_manifest(cmanifest_path, chunk_manifest)
    print(f"✅ Chunk manifest saved to: {cmanifest_path}")

    skip_chunks = {x.strip() for x in args.skip_chunks.split(",") if x.strip()}
    if skip_chunks:
        before = len(chunk_files)
        chunk_files = [p for p in chunk_files if p.name not in skip_chunks]
        print(f"\nSkip {before - len(chunk_files)} user-specified chunks; {len(chunk_files)} chunk(s) left to submit.")

    if not chunk_files:
        print("No chunks left to submit after skipping.")
        if args.download_results:
            if OpenAI is None:
                print("❌ OpenAI library not installed, cannot download results.")
                sys.exit(1)
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                print("❌ OPENAI_API_KEY not found, cannot download results.")
                sys.exit(1)
            client = OpenAI(api_key=api_key)
            download_and_materialize_results(client, batch_jsonl_path, mapping_path)
        sys.exit(0)

    if not args.submit_batch:
        print("\n🛑 Dry run complete. Files are ready on disk.")
        if args.download_results:
            if OpenAI is None:
                print("❌ OpenAI library not installed, cannot download results.")
                sys.exit(1)
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                print("❌ OPENAI_API_KEY not found, cannot download results.")
                sys.exit(1)
            client = OpenAI(api_key=api_key)
            download_and_materialize_results(client, batch_jsonl_path, mapping_path)
        sys.exit(0)

    if OpenAI is None:
        print("❌ OpenAI library not installed.")
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("❌ OPENAI_API_KEY not found.")
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    MAX_CONCURRENT_BATCHES = 3
    GROUP_POLL_INTERVAL = 120
    jobs = {}

    print(f"\n🚀 Submitting {len(chunk_files)} chunk(s) in groups of {MAX_CONCURRENT_BATCHES}...")

    for start in range(0, len(chunk_files), MAX_CONCURRENT_BATCHES):
        group = chunk_files[start:start + MAX_CONCURRENT_BATCHES]
        group_jobs = {}

        print(f"\n=== Submitting group {start // MAX_CONCURRENT_BATCHES + 1} ({len(group)} chunk(s)) ===")

        for fpath in group:
            chunk_meta = chunk_manifest.get(fpath.name, {})
            prompt_ids = chunk_meta.get("prompt_ids", [])

            job_id = upload_and_run_batch(client, fpath)
            if job_id:
                group_jobs[fpath.name] = job_id
                jobs[fpath.name] = job_id

                mark_prompts_submitted(prompt_state, prompt_ids, args.mode, fpath.name, job_id)
                save_prompt_state(state_path, prompt_state)

                append_jsonl(events_path, {
                    "event": "submit",
                    "ts": utc_now_iso(),
                    "mode": args.mode,
                    "chunk_file": fpath.name,
                    "job_id": job_id,
                    "prompt_ids": prompt_ids,
                })
                persist_job_log(batch_jsonl_path, args.mode, jobs)
            else:
                append_jsonl(events_path, {
                    "event": "submit_failed",
                    "ts": utc_now_iso(),
                    "mode": args.mode,
                    "chunk_file": fpath.name,
                })
                print(f"⚠️ Failed to submit chunk {fpath.name}.")

        if not group_jobs:
            print("⚠️ No jobs submitted successfully in this group; skipping wait.")
            continue

        print(f"⏳ Waiting for current group ({len(group_jobs)} job(s)) to finish...")
        while True:
            all_done = True
            status_counts = {}

            for fname, job_id in group_jobs.items():
                try:
                    batch = client.batches.retrieve(job_id)
                    status = batch.status
                except Exception as e:
                    status = f"error:{e}"

                status_counts[status] = status_counts.get(status, 0) + 1

                prompt_ids = chunk_manifest.get(fname, {}).get("prompt_ids", [])
                mark_prompts_status(prompt_state, prompt_ids, status)
                save_prompt_state(state_path, prompt_state)

                append_jsonl(events_path, {
                    "event": "status",
                    "ts": utc_now_iso(),
                    "chunk_file": fname,
                    "job_id": job_id,
                    "status": status,
                })

                if status not in ("completed", "failed", "cancelled", "expired"):
                    all_done = False

            status_str = ", ".join(f"{k}: {v}" for k, v in status_counts.items())
            print(f"   Group status: {status_str}")

            if all_done:
                print("   ✅ Current group finished. Proceeding to next group.")
                break

            print(f"   Sleeping {GROUP_POLL_INTERVAL}s before next status check...")
            time.sleep(GROUP_POLL_INTERVAL)

    if jobs:
        persist_job_log(batch_jsonl_path, args.mode, jobs)
        job_log = batch_jsonl_path.parent / f"batch_jobs_{args.mode}.json"
        print(f"\n✅ Job IDs saved to: {job_log} (also updated after each upload)")
        print(f"✅ Prompt state saved to: {state_path}")
        print(f"✅ Submission events saved to: {events_path}")
        if args.mode == "preview":
            print("\n*** PREVIEW SUBMITTED ***")
            print("If results look good, run again with --mode all --submit-batch true")
    else:
        print("❌ No jobs were successfully submitted; no job log written.")

    if args.download_results:
        client = OpenAI(api_key=api_key)
        download_and_materialize_results(client, batch_jsonl_path, mapping_path)


if __name__ == "__main__":
    main()
