#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
fallback_openai_search.py

Last-resort AS2Biz classification. For organisations still unclassified after the
website, sibling-inheritance, and Wikipedia passes, query an LLM with a built-in
web-search tool using only the organisation name + country code, and parse the
structured-JSON category list it returns.

Inputs (produced by `post_process.py wiki`):
  <result_root>/<date>/fallback_web_search/fallback_extra_query_orgs.json
      JSON list of "Org Name (CC)" strings
  <result_root>/<date>/fallback_web_search/orgname2asn.json
      { "Org Name (CC)": ["<asn>", ...] }

Outputs:
  <result_root>/<date>/fallback_web_search/_cache_classify.json      (resume cache)
  <result_root>/<date>/fallback_web_search/org_classification_raw.json
  <result_root>/<date>/fallback_web_search/org_classification_parsed.json
  <result_root>/<date>/fallback_web_search/asn_classification_parsed.json

Environment:
  OPENAI_API_KEY   required
  OPENAI_BASE_URL  optional; defaults to https://api.openai.com/v1. Set it to
                   point at any OpenAI-compatible Responses API endpoint.

The prompt and taxonomy come from as2biz/prompt.py
(`descr` + `fallback_web_search_prompt`, `taxonomy_list`).
"""

import argparse
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List
import random

import requests

from prompt import descr as DESCR, fallback_web_search_prompt, taxonomy_list

# TAXONOMY is the list of valid category names (no "Cannot determine" sentinel).
TAXONOMY = list(taxonomy_list)

# Full instruction block: shared disambiguation text + the fallback task prompt.
FULL_PROMPT_TEMPLATE = DESCR + "\n\n" + fallback_web_search_prompt


def parse_structured_result(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    text = extract_output_text(resp_json)
    if not text:
        return {
            "categories": [],
            "cannot_determine": False,
            "parse_error": "empty_output_text",
        }

    try:
        obj = json.loads(text)
    except Exception as e:
        return {
            "categories": [],
            "cannot_determine": False,
            "parse_error": f"json_decode_error: {e}",
            "raw_text": text,
        }

    if not isinstance(obj, dict):
        return {
            "categories": [],
            "cannot_determine": False,
            "parse_error": "json_not_object",
            "raw_text": text,
        }

    categories = obj.get("categories", [])
    cannot_determine = obj.get("cannot_determine", False)

    if not isinstance(categories, list):
        return {
            "categories": [],
            "cannot_determine": False,
            "parse_error": "categories_not_list",
            "raw_text": text,
        }

    if not isinstance(cannot_determine, bool):
        return {
            "categories": [],
            "cannot_determine": False,
            "parse_error": "cannot_determine_not_bool",
            "raw_text": text,
        }

    bad = [c for c in categories if c not in TAXONOMY]
    if bad:
        return {
            "categories": [],
            "cannot_determine": False,
            "parse_error": f"invalid_categories: {bad}",
            "raw_text": text,
        }

    if cannot_determine:
        return {
            "categories": ["Cannot determine categories"],
            "cannot_determine": True,
            "parse_error": None,
        }

    return {
        "categories": categories,
        "cannot_determine": False,
        "parse_error": None,
    }

# =========================
# Config / helpers
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

FILE_LOCK = threading.Lock()


def write_parsed_outputs(
    *,
    orgs: List[str],
    orgname2asn: Dict[str, List[str]],
    cache: Dict[str, Any],
    raw_out_path: Path,
    parsed_org_out_path: Path,
    parsed_asn_out_path: Path,
) -> None:
    raw_by_org: Dict[str, Any] = {}
    parsed_by_org: Dict[str, List[str]] = {}
    parsed_by_asn: Dict[str, Dict[str, Any]] = {}

    for org in orgs:
        key = sha1_text(org)
        rec = cache.get(key, {})
        raw_by_org[org] = {
            "ok": rec.get("ok"),
            "status_code": rec.get("status_code"),
            "elapsed": rec.get("elapsed"),
            "output_text": rec.get("output_text"),
            "error": rec.get("error"),
            "model": rec.get("model"),
            "service_tier": rec.get("service_tier"),
        }
        parsed_by_org[org] = rec.get("parsed_categories", [])

        for asn in orgname2asn.get(org, []):
            parsed_by_asn[str(asn)] = {
                "org_with_cc": org,
                "categories": rec.get("parsed_categories", []),
                "ok": rec.get("ok"),
            }

    dump_json_atomic(raw_by_org, raw_out_path)
    dump_json_atomic(parsed_by_org, parsed_org_out_path)
    dump_json_atomic(parsed_by_asn, parsed_asn_out_path)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json_atomic(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8"), usedforsecurity=False).hexdigest()


def extract_output_text(resp_json: Dict[str, Any]) -> str:
    if not isinstance(resp_json, dict):
        return ""
    if resp_json.get("output_text"):
        return resp_json["output_text"]

    texts: List[str] = []
    for item in resp_json.get("output", []):
        if item.get("type") != "message":
            continue
        for c in item.get("content", []):
            if c.get("type") in ("output_text", "text") and "text" in c:
                texts.append(c["text"])
    return "\n".join(texts).strip()


def build_prompt(org_with_cc: str) -> str:
    return FULL_PROMPT_TEMPLATE.format(org_with_cc)


def get_api_base() -> str:
    return os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def get_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def call_responses_api(
    prompt: str,
    *,
    model: str,
    max_output_tokens: int,
    use_flex: bool,
    timeout: float = 180.0,
    retries: int = 1,
) -> Dict[str, Any]:
    """
    Responses API call with the built-in web_search tool and a strict JSON schema.
    `service_tier="flex"` trades latency for lower cost when supported.
    """
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    url = f"{get_api_base()}/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: Dict[str, Any] = {
        "model": model,
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "max_output_tokens": max_output_tokens,
        "max_tool_calls": 3,
        "temperature": 0,
        "prompt_cache_key": sha1_text(FULL_PROMPT_TEMPLATE[:1000]),
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "org_classification",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "categories": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": TAXONOMY
                            }
                        },
                        "cannot_determine": {
                            "type": "boolean"
                        }
                    },
                    "required": ["categories", "cannot_determine"],
                    "additionalProperties": False
                }
            }
        }
    }
    if use_flex:
        payload["service_tier"] = "flex"

    last_err = None

    for attempt in range(retries + 1):
        if attempt > 0:
            sleep_s = min(60.0, 2 ** attempt) + random.uniform(0, 1.5)
            time.sleep(sleep_s)

        started = time.time()
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            elapsed = time.time() - started

            ctype = r.headers.get("Content-Type", "")
            body: Any
            if "application/json" in ctype:
                body = r.json()
            else:
                body = {"raw_text": r.text}

            ok = 200 <= r.status_code < 300
            if not ok:
                preview = r.text[:400].replace("\n", " ")
                log.warning("HTTP %s body=%r", r.status_code, preview)

            return {
                "ok": ok,
                "status_code": r.status_code,
                "elapsed": elapsed,
                "response_json": body,
                "output_text": extract_output_text(body) if isinstance(body, dict) else "",
                "error": None if ok else preview,
            }
        except Exception as e:
            last_err = str(e)

    return {
        "ok": False,
        "status_code": None,
        "elapsed": None,
        "response_json": None,
        "output_text": "",
        "error": last_err or "unknown error",
    }


def worker(
    org_with_cc: str,
    *,
    cache: Dict[str, Any],
    cache_path: Path,
    model: str,
    max_output_tokens: int,
    use_flex: bool,
    timeout: float,
) -> None:
    key = sha1_text(org_with_cc)

    with FILE_LOCK:
        existing = cache.get(key)
        if existing and existing.get("done"):
            return

    prompt = build_prompt(org_with_cc)
    res = call_responses_api(
        prompt,
        model=model,
        max_output_tokens=max_output_tokens,
        use_flex=use_flex,
        timeout=timeout,
    )
    parsed = parse_structured_result(res["response_json"] or {})
    resp_json = res["response_json"] or {}
    output_types = [item.get("type") for item in resp_json.get("output", [])]

    response_status = resp_json.get("status")

    done = bool(
        res["ok"]
        and response_status == "completed"
        and parsed["parse_error"] is None
    )
    record = {
        "done": done,
        "org_with_cc": org_with_cc,
        "prompt_sha1": sha1_text(prompt),
        "ok": res["ok"],
        "status_code": res["status_code"],
        "elapsed": res["elapsed"],
        "output_text": res["output_text"],
        "parsed_categories": parsed["categories"],
        "cannot_determine": parsed["cannot_determine"],
        "parse_error": parsed["parse_error"],
        "parsed_raw_text": parsed.get("raw_text"),
        "response_json": res["response_json"],
        "error": res["error"],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model,
        "service_tier": "flex" if use_flex else "default",
        "response_id": resp_json.get("id"),
        "response_status": resp_json.get("status"),
        "incomplete_details": resp_json.get("incomplete_details"),
        "usage": resp_json.get("usage"),
        "output_types": output_types,
    }

    with FILE_LOCK:
        cache[key] = record
        dump_json_atomic(cache, cache_path)

    mark = "✓" if res["ok"] else "✗"
    preview = (res["output_text"] or "")[:120].replace("\n", " ")
    log.info("%s %s status=%s elapsed=%s text=%r",
             mark, org_with_cc, res["status_code"], res["elapsed"], preview)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--result_root", default="./result")
    parser.add_argument("--model", default="gpt-5.2",
                        help="Model with web_search support. Model names drift; "
                             "set a current one.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max_output_tokens", type=int, default=768)
    parser.add_argument("--max_new", type=int, default=0, help="0 means no limit")
    parser.add_argument("--no_resume", action="store_true")
    parser.add_argument("--no_flex", action="store_true")
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument("--timeout", type=float, default=900.0)  # flex tier can be slow
    parser.add_argument(
        "--parse_cache",
        action="store_true",
        help="Only read existing cache and regenerate raw/parsed output files; do not make any API calls.",
    )
    args = parser.parse_args()

    base = Path(args.result_root) / args.date / "fallback_web_search"
    orgs_path = base / "fallback_extra_query_orgs.json"
    orgname2asn_path = base / "orgname2asn.json"

    cache_path = base / "_cache_classify.json"
    raw_out_path = base / "org_classification_raw.json"
    parsed_org_out_path = base / "org_classification_parsed.json"
    parsed_asn_out_path = base / "asn_classification_parsed.json"

    orgs: List[str] = load_json(orgs_path)
    orgname2asn: Dict[str, List[str]] = load_json(orgname2asn_path)

    if not isinstance(orgs, list):
        raise ValueError(f"{orgs_path} is not a JSON list")
    if not isinstance(orgname2asn, dict):
        raise ValueError(f"{orgname2asn_path} is not a JSON object")

    cache: Dict[str, Any] = {} if args.no_resume else (load_json(cache_path) if cache_path.exists() else {})

    if args.parse_cache:
        if not cache_path.exists():
            raise FileNotFoundError(f"Cache file not found: {cache_path}")

        log.info("parse_cache mode: regenerating outputs from existing cache only")
        log.info("Loaded %d cache entries", len(cache))

        write_parsed_outputs(
            orgs=orgs,
            orgname2asn=orgname2asn,
            cache=cache,
            raw_out_path=raw_out_path,
            parsed_org_out_path=parsed_org_out_path,
            parsed_asn_out_path=parsed_asn_out_path,
        )

        log.info("Done (parse_cache only)")
        log.info("Raw responses:   %s", raw_out_path)
        log.info("Parsed by org:   %s", parsed_org_out_path)
        log.info("Parsed by ASN:   %s", parsed_asn_out_path)
        return

    log.info("Loaded %d deduped orgs", len(orgs))
    log.info("Loaded %d org->ASN mappings", len(orgname2asn))
    log.info("Cache entries: %d", len(cache))

    to_run = []
    for org in orgs:
        key = sha1_text(org)
        done = cache.get(key, {}).get("done", False)
        if not done:
            to_run.append(org)

    if args.max_new > 0:
        to_run = to_run[:args.max_new]

    log.info("Need new API calls: %d", len(to_run))

    if args.dry_run:
        return

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [
            ex.submit(
                worker,
                org,
                cache=cache,
                cache_path=cache_path,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                use_flex=not args.no_flex,
                timeout=args.timeout,
            )
            for org in to_run
        ]
        for fut in as_completed(futures):
            fut.result()

    # Build output views
    write_parsed_outputs(
        orgs=orgs,
        orgname2asn=orgname2asn,
        cache=cache,
        raw_out_path=raw_out_path,
        parsed_org_out_path=parsed_org_out_path,
        parsed_asn_out_path=parsed_asn_out_path,
    )

    log.info("Done")
    log.info("Raw responses:   %s", raw_out_path)
    log.info("Parsed by org:   %s", parsed_org_out_path)
    log.info("Parsed by ASN:   %s", parsed_asn_out_path)
    log.info("Cache:           %s", cache_path)


if __name__ == "__main__":
    main()
