#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import argparse
import json
import os
import sys
import io
import gzip
import re
import html
import math
import signal
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict, Counter
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
        template_singlemodal_202609,
        taxonomy_with_ids_202609,
        descr_with_category_ids_202609,
        classification_instructions,
        classification_instructions_before_202609,
        category_id_to_name_202609,
        taxonomy_list,
    )
    HAVE_PROMPT_TEMPLATE = True
except ImportError:
    raise
    HAVE_PROMPT_TEMPLATE = False

import as2biz_model_routing as routing
import as2biz_routing_features as routing_features

load_dotenv()

PROMPT_CONTRACT = "2026-09-v2-category-ids"
LEGACY_PROMPT_CONTRACT = "pre-2026-09-full-names"
PROMPT_CONTRACT_CUTOFF = (2026, 9, 1)
WEBSITE_ISSUE_CATEGORY_ID = "C097"
WEBSITE_ISSUE_CATEGORY_NAME = "Website issue - Cannot determine categories"


@dataclass(frozen=True)
class PromptContract:
    name: str
    developer_prompt: str
    user_template: str
    taxonomy: str
    descriptions: str
    structured_output: bool
    supports_versioned_routing: bool


PROMPT_CONTRACTS = {
    LEGACY_PROMPT_CONTRACT: PromptContract(
        name=LEGACY_PROMPT_CONTRACT,
        developer_prompt=classification_instructions_before_202609,
        user_template=template_singlemodal,
        taxonomy=taxonomy,
        descriptions=descr,
        structured_output=False,
        supports_versioned_routing=False,
    ),
    PROMPT_CONTRACT: PromptContract(
        name=PROMPT_CONTRACT,
        developer_prompt=classification_instructions,
        user_template=template_singlemodal_202609,
        taxonomy=taxonomy_with_ids_202609,
        descriptions=descr_with_category_ids_202609,
        structured_output=True,
        supports_versioned_routing=True,
    ),
}


def _snapshot_date(version_tag: str) -> tuple[int, int, int]:
    """Extract YYYY-MM[-DD] (or YYYYMM[DD]) from a snapshot version tag."""
    raw = str(version_tag or "").strip()
    m = re.search(r"(?<!\d)(\d{4})[-_]?([01]\d)(?:[-_]?([0-3]\d))?(?!\d)", raw)
    if not m:
        raise ValueError(
            f"cannot infer a snapshot date from version tag {version_tag!r}; "
            "pass --prompt-contract explicitly"
        )
    year, month = map(int, m.groups()[:2])
    day = int(m.group(3) or 1)
    try:
        datetime(year, month, day)
    except ValueError as e:
        raise ValueError(f"invalid snapshot date in version tag {version_tag!r}: {e}") from e
    return year, month, day


def resolve_prompt_contract(version_tag: str, requested: str = "auto") -> PromptContract:
    """Resolve the immutable prompt/output contract for one snapshot."""
    if requested != "auto":
        try:
            return PROMPT_CONTRACTS[requested]
        except KeyError as e:
            raise ValueError(f"unknown prompt contract: {requested!r}") from e
    name = (PROMPT_CONTRACT
            if _snapshot_date(version_tag) >= PROMPT_CONTRACT_CUTOFF
            else LEGACY_PROMPT_CONTRACT)
    return PROMPT_CONTRACTS[name]

#: Encoding for the routing prompt-length metric. GPT-5.6 uses o200k_base
#: (the gpt-4o / gpt-5 family encoding); count_tokens()'s cl100k_base fallback
#: mis-tokenizes non-Latin scripts badly (calibration: cl100k framing stdev
#: ~3200 tokens vs o200k stdev 0). Do NOT route on count_tokens().
ROUTING_TOKENIZER_ENCODING = "o200k_base"

#: Per-request chat-framing overhead (role markers, message boundaries,
#: response_format scaffolding, ...) that the API adds on top of the encoded
#: message text. Measured on the 100-site paired evaluation: with o200k_base
#: this is an exact constant, api_prompt_tokens - sum(encoded parts) == 341
#: for all 100 requests. This is the calibrated default for
#: --routing-framing-overhead-tokens; re-measure if the request shape or the
#: model's chat template changes.
DEFAULT_ROUTING_FRAMING_OVERHEAD_TOKENS = 341

# Default Batch prices in USD per one million tokens. Operators can override
# either model with --model-price, or provide a shared fallback for a different
# model with --batch-input-price-per-1m + --batch-output-price-per-1m.
DEFAULT_MODEL_PRICES = {
    routing.MODEL_LUNA: (0.20, 1.20),
    routing.MODEL_TERRA: (2.00, 12.00),
}

_routing_encoding = None


def routing_token_count(text: str) -> int:
    """Token count for routing decisions only (o200k_base). Separate from
    count_tokens(), which drives prompt-body budgeting and may use a different
    encoding."""
    global _routing_encoding
    if _routing_encoding is None:
        _routing_encoding = tiktoken.get_encoding(ROUTING_TOKENIZER_ENCODING)
    return len(_routing_encoding.encode(text or ""))


def response_format_schema():
    """The strict category-ID Structured Outputs schema for the 2026-09
    contract. Single source of truth: used both to build each request and to
    fingerprint the contract in the build signature. Array uniqueness is
    enforced in application code (extract_valid_categories), not with
    uniqueItems, since that keyword is outside the documented strict subset."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "as2biz_category_ids_202609",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "category_ids": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(category_id_to_name_202609),
                        },
                    }
                },
                "required": ["category_ids"],
                "additionalProperties": False,
            },
        },
    }


def contract_fingerprint(prompt_contract: str = PROMPT_CONTRACT):
    """SHA-256 of each selected contract component. Folded into the
    build signature so a prompt-, taxonomy-ID-, description-, schema-, or
    routing-policy-only change invalidates a stale manifest."""
    def h(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    contract = PROMPT_CONTRACTS[prompt_contract]
    return {
        "contract": contract.name,
        "routing_policy": (routing.ROUTING_POLICY_VERSION
                           if contract.supports_versioned_routing else None),
        "developer_prompt_sha256": h(contract.developer_prompt),
        "user_template_sha256": h(contract.user_template),
        "taxonomy_sha256": h(contract.taxonomy),
        "descriptions_sha256": h(contract.descriptions),
        "response_schema_sha256": (
            h(json.dumps(response_format_schema(), sort_keys=True, ensure_ascii=False))
            if contract.structured_output else None
        ),
    }


def build_classification_task(
    *,
    custom_id: str,
    model: str,
    developer_prompt: str,
    stable_user_prefix: str,
    variable_body: str,
    temperature: float,
    max_completion_tokens: int,
    prompt_contract: str = PROMPT_CONTRACT,
):
    """Assemble one OpenAI Batch request for the selected prompt contract.

    Single source of truth for request shape, shared by single-model and
    routed builds. For GPT-5.6 the stable prefix (developer instructions +
    ``stable_user_prefix``) is placed before an explicit prompt-cache
    breakpoint and only ``variable_body`` follows it; the prefix must be
    byte-identical across every request sent to the same model, so no
    hostname/ASN/date/prompt-id may appear in ``developer_prompt`` or
    ``stable_user_prefix``. For non-5.6 models the user message is a single
    flat string and no cache fields are emitted.
    """
    use_explicit_cache = routing.is_gpt_56(model)

    if use_explicit_cache:
        user_content = [
            {
                "type": "text",
                "text": stable_user_prefix,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            },
            {"type": "text", "text": variable_body},
        ]
    else:
        user_content = stable_user_prefix + variable_body

    contract = PROMPT_CONTRACTS[prompt_contract]
    body = {
        "model": model,
        "temperature": temperature,
        "max_completion_tokens": max_completion_tokens,
        "reasoning_effort": "none",
        "messages": [
            {"role": "developer", "content": developer_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    if contract.structured_output:
        body["response_format"] = response_format_schema()

    if use_explicit_cache:
        model_cache_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", model)
        body["prompt_cache_key"] = f"as2biz-{prompt_contract}-{model_cache_slug}"
        body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}

    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": body,
    }


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


def utc_now_compact():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


try:
    import fcntl as _fcntl
except ImportError:                       # non-POSIX; the lock becomes a no-op
    _fcntl = None


class routed_dir_lock:
    """Best-effort exclusive lock over a routed directory, so two concurrent
    ``--submit-batch`` runs cannot both decide the same part is unsubmitted and
    double-create a paid Batch. Non-blocking: a second holder aborts rather
    than queues. No-op where ``fcntl`` is unavailable."""

    def __init__(self, routed_dir: Path):
        self.path = routed_dir / ".submit.lock"
        self._fh = None

    def __enter__(self):
        if _fcntl is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            _fcntl.flock(self._fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                f"another submit is already running for this routed directory "
                f"(lock held on {self.path}). Wait for it to finish, or remove "
                f"the lock file if you are sure no other run is active."
            )
        self._fh.write(f"{os.getpid()} {utc_now_iso()}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                _fcntl.flock(self._fh, _fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False


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


ARTIFACT_HASHES_VERSION = 1

# GPT-5.6 bills the first write of a cacheable prefix at 1.25x the uncached
# input rate (OpenAI prompt-caching guide). The pre-submit "$ ceiling" prices
# ALL estimated input at this rate so it stays a real upper bound even when a
# request writes the cached prefix rather than reading it.
CACHE_WRITE_PREMIUM = 1.25


def _custom_ids_sha256(custom_ids) -> str:
    return hashlib.sha256(
        "\n".join(sorted(str(c) for c in custom_ids)).encode("utf-8")
    ).hexdigest()


def verify_routed_artifacts(out_dir: Path, manifest: dict) -> None:
    """Re-hash the on-disk routed artifacts and compare to the SHA-256 values
    the manifest recorded at build time. Raises RuntimeError on any missing
    file, content mismatch, or request-count/custom-id-set mismatch.

    This is what protects the *reuse-then-submit* path: an unchanged build
    signature means ``write_routed_artifacts`` returns without re-validating
    the part files, and ``submit_routed_artifacts`` uploads them verbatim, so
    corruption or a manual edit between build and submit would otherwise go
    straight to a paid Batch. A manifest written before
    ``ARTIFACT_HASHES_VERSION`` carries no hashes and cannot be integrity
    checked -- that is a hard error, not a warning: the operator must rebuild
    it with ``--force-resubmit`` before submitting."""
    if manifest.get("artifact_hashes_version") != ARTIFACT_HASHES_VERSION:
        raise RuntimeError(
            "routed manifest predates artifact hashing "
            f"(artifact_hashes_version={manifest.get('artifact_hashes_version')!r}, "
            f"need {ARTIFACT_HASHES_VERSION}); its part files cannot be integrity "
            "checked before upload. Rebuild the routed directory with "
            "--force-resubmit, then submit."
        )

    problems = []

    mp = out_dir / "routing_mapping.json"
    want_map = manifest.get("mapping_sha256")
    if not mp.exists():
        problems.append("routing_mapping.json is missing")
    elif want_map and _sha256_file(mp) != want_map:
        problems.append("routing_mapping.json content does not match the manifest hash")

    for model, info in (manifest.get("per_model") or {}).items():
        model_dir = out_dir / model
        files = info.get("files") or {}
        if not files:
            problems.append(f"{model}: manifest records no file hashes")
        for fname, want in files.items():
            fp = model_dir / fname
            if not fp.exists():
                problems.append(f"{model}/{fname} is missing")
            elif _sha256_file(fp) != want:
                problems.append(f"{model}/{fname} content does not match the manifest hash")
        # part files present on disk but absent from the manifest -> also a mismatch
        for extra in sorted(model_dir.glob("batch_input.part*.jsonl")):
            if extra.name not in files:
                problems.append(f"{model}/{extra.name} is on disk but not in the manifest")
        # request count / custom-id set from the reassembled part files
        want_n = info.get("request_count")
        want_cids = info.get("custom_ids_sha256")
        if want_n is not None or want_cids is not None:
            seen = []
            for pf in sorted(model_dir.glob("batch_input.part*.jsonl")):
                try:
                    for ln in pf.read_text(encoding="utf-8").splitlines():
                        ln = ln.strip()
                        if ln:
                            seen.append(json.loads(ln).get("custom_id"))
                except Exception as e:
                    problems.append(f"{model}/{pf.name} is not readable JSONL ({e})")
            if want_n is not None and len(seen) != want_n:
                problems.append(f"{model}: {len(seen)} requests on disk != {want_n} in manifest")
            if want_cids is not None and _custom_ids_sha256(seen) != want_cids:
                problems.append(f"{model}: custom-id set on disk does not match the manifest")

    if problems:
        raise RuntimeError(
            "routed artifact integrity check failed for "
            f"{out_dir}:\n  - " + "\n  - ".join(problems)
        )


def write_routed_artifacts(
    all_task_lines,
    final_mapping,
    out_dir: Path,
    build_sig: str,
    tokens_per_batch_cap: int,
    force: bool = False,
):
    """Partition routed (Luna/Terra) prompts into isolated per-model artifacts
    under ``out_dir`` and enforce the cross-stream invariants from the
    deployment plan. Returns the routing manifest dict. Raises RuntimeError on
    any invariant violation so a bad build cannot proceed to submission.

    Overwrite policy, keyed on the existing routing_manifest.json's
    ``build_signature``:
      * different build_signature -> raise unless ``force`` (never silently
        clobber another run's artifacts, submitted or not);
      * same build_signature -> idempotent no-op: return the existing manifest
        without rewriting anything;
      * ``force`` -> rebuild regardless.

    Layout::

        out_dir/
          routing_manifest.json          shared: per-model counts, hashes, versions
          routing_mapping.json           shared prompt->ASN map (each record has "model")
          gpt-5.6-luna/
            batch_input.jsonl
            batch_input_chunk_manifest.json
          gpt-5.6-terra/
            ...
    """
    contract_fp = contract_fingerprint()
    routing_version = routing.ROUTING_POLICY_VERSION

    existing_manifest_p = out_dir / "routing_manifest.json"
    if existing_manifest_p.exists() and not force:
        existing = safe_read_json(existing_manifest_p, default={}) or {}
        if existing.get("build_signature") != build_sig:
            submitted = sorted(p.parent.name for p in out_dir.glob("*/batch_jobs.json"))
            raise RuntimeError(
                f"routed build: {out_dir} already holds artifacts for a different "
                f"build_signature ({str(existing.get('build_signature'))[:12]} != "
                f"{build_sig[:12]}"
                + (f", submitted jobs for {submitted}" if submitted else "")
                + "). Pass --force-resubmit to overwrite, or use a fresh "
                "--routing-output-dir."
            )
        # same build_signature -> we are about to reuse the part files on disk
        # as-is (and submit_routed_artifacts will upload them verbatim). The
        # first-write invariants below never re-run on this path, so integrity
        # is re-checked here against the hashes recorded at build time.
        verify_routed_artifacts(out_dir, existing)
        print(f"    routed artifacts for build {build_sig[:12]} already present in "
              f"{out_dir}; reusing (verified, no rewrite).")
        return existing

    # force rebuild: the part files about to be written are new, so any job
    # records for the previous part files no longer correspond. Supersede them
    # now -- BEFORE writing new artifacts and regardless of whether submission
    # follows -- so a later plain submit cannot attach a stale job to fresh
    # artifacts.
    if force:
        ts = utc_now_compact()
        for jp in out_dir.glob("*/batch_jobs.json"):
            jp.replace(jp.with_name(f"batch_jobs.superseded-{ts}.json"))
            print(f"    --force: {jp.parent.name}/{jp.name} -> "
                  f"batch_jobs.superseded-{ts}.json")

    by_model: dict[str, list] = defaultdict(list)
    seen_custom_ids: dict[str, str] = {}
    for item in all_task_lines:
        model = item.get("model")
        cid = item["prompt_id"]
        if model is None:
            raise RuntimeError(f"routed build: task {cid} has no model")
        if cid in seen_custom_ids:
            raise RuntimeError(
                f"routed build: duplicate custom_id {cid} "
                f"(streams {seen_custom_ids[cid]} and {model})"
            )
        # invariant: the request line's own custom_id / model match the task
        # metadata (a corrupted reused artifact would otherwise be submitted
        # and only caught, after paying, at materialization)
        try:
            body_obj = json.loads(item["line"])
        except Exception:
            raise RuntimeError(f"routed build: task {cid} line is not valid JSON")
        if body_obj.get("custom_id") != cid:
            raise RuntimeError(
                f"routed build: line custom_id {body_obj.get('custom_id')!r} != task {cid!r}")
        line_model = (body_obj.get("body") or {}).get("model")
        if line_model != model:
            raise RuntimeError(
                f"routed build: task {cid} line model {line_model!r} != task model {model!r}")
        # invariant: the mapping agrees on this prompt's model
        m_meta = final_mapping.get(cid)
        if m_meta is None:
            raise RuntimeError(f"routed build: task {cid} has no mapping entry")
        if m_meta.get("model") != model:
            raise RuntimeError(
                f"routed build: task {cid} model {model!r} != mapping model "
                f"{m_meta.get('model')!r}")
        seen_custom_ids[cid] = model
        by_model[model].append(item)

    # invariant: streams partition the prompt set exactly
    total = sum(len(v) for v in by_model.values())
    if total != len(all_task_lines):
        raise RuntimeError(
            f"routed build: per-model total {total} != unique prompt count {len(all_task_lines)}"
        )

    # invariant: every mapped ASN points at a routed prompt in exactly one stream
    routed_pids = set(seen_custom_ids)
    asn_stream: dict[str, str] = {}
    for pid, meta in final_mapping.items():
        if pid not in routed_pids:
            raise RuntimeError(f"routed build: mapping prompt {pid} is not in any model stream")
        model = meta.get("model")
        for asn in meta.get("asns", []):
            prev = asn_stream.get(str(asn))
            if prev is not None and prev != model:
                raise RuntimeError(
                    f"routed build: ASN {asn} maps to both {prev} and {model} streams"
                )
            asn_stream[str(asn)] = model

    out_dir.mkdir(parents=True, exist_ok=True)
    per_model = {}
    for model, items in sorted(by_model.items()):
        model_dir = out_dir / model
        model_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = model_dir / "batch_input.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(item["line"] + "\n")

        chunks = plan_batch_chunks(items, tokens_per_batch_cap)
        # remove any stale part files from a previous build of this stream
        for old_part in model_dir.glob("batch_input.part*.jsonl"):
            old_part.unlink()
        chunk_entries = []
        for i, ch in enumerate(chunks, 1):
            n = len(ch["tasks"])
            if n > MAX_REQUESTS_PER_BATCH:
                raise RuntimeError(f"{model} chunk {i}: {n} requests > {MAX_REQUESTS_PER_BATCH}")
            if ch["est_bytes"] > MAX_BYTES_PER_BATCH:
                raise RuntimeError(f"{model} chunk {i}: {ch['est_bytes']} bytes over limit")
            if ch["est_tokens"] > tokens_per_batch_cap:
                raise RuntimeError(f"{model} chunk {i}: {ch['est_tokens']} tokens over cap")
            part_path = model_dir / f"batch_input.part{i}.jsonl"
            with open(part_path, "w", encoding="utf-8") as pf:
                for line in ch["tasks"]:
                    pf.write(line + "\n")
            chunk_entries.append({
                "part": i,
                "file": part_path.name,
                "requests": n,
                "est_tokens": ch["est_tokens"],
                "est_bytes": ch["est_bytes"],
            })

        # reserved completion budget: sum each request's own
        # max_completion_tokens, never count x max (which is wrong the moment
        # the budget varies per request, and mis-prices output tokens)
        completion_budget = sum(
            int(final_mapping.get(it["prompt_id"], {}).get("max_completion_tokens", 0) or 0)
            for it in items
        )
        # SHA-256 every file a later reuse/submit will upload as-is
        file_hashes = {jsonl_path.name: _sha256_file(jsonl_path)}
        for ce in chunk_entries:
            fp = model_dir / ce["file"]
            file_hashes[ce["file"]] = _sha256_file(fp)
        chunk_manifest = {
            "model": model,
            "build_id": f"{build_sig[:12]}__{model}__{routing_version}",
            "prompt_contract": PROMPT_CONTRACT,
            "routing_policy_version": routing_version,
            "batch_file": str(jsonl_path),
            "prompt_count": len(items),
            "asn_count": sum(
                len(final_mapping.get(it["prompt_id"], {}).get("asns", [])) for it in items
            ),
            # est_tokens already includes the reserved completion budget; split
            # it out so a cost estimate can price input and output separately
            "est_total_tokens": sum(ch["est_tokens"] for ch in chunks),
            "est_completion_budget_tokens": completion_budget,
            "request_count": len(items),
            "custom_ids_sha256": _custom_ids_sha256(it["prompt_id"] for it in items),
            "files": file_hashes,
            "chunks": chunk_entries,
        }
        safe_write_json(model_dir / "batch_input_chunk_manifest.json", chunk_manifest)
        per_model[model] = chunk_manifest

    safe_write_json(out_dir / "routing_mapping.json", final_mapping)

    manifest = {
        "created_at": utc_now_iso(),
        "build_signature": build_sig,
        "routed_dir": str(out_dir.resolve()),
        "prompt_contract": PROMPT_CONTRACT,
        "routing_policy_version": routing_version,
        "contract_fingerprint": contract_fp,
        "artifact_hashes_version": ARTIFACT_HASHES_VERSION,
        "mapping_sha256": _sha256_file(out_dir / "routing_mapping.json"),
        "total_prompts": len(all_task_lines),
        "total_asns": sum(len(m.get("asns", [])) for m in final_mapping.values()),
        "per_model": {
            m: {
                "prompt_count": cm["prompt_count"],
                "asn_count": cm["asn_count"],
                "chunk_count": len(cm["chunks"]),
                "est_total_tokens": cm["est_total_tokens"],
                "est_completion_budget_tokens": cm["est_completion_budget_tokens"],
                "request_count": cm["request_count"],
                "custom_ids_sha256": cm["custom_ids_sha256"],
                "files": cm["files"],
            }
            for m, cm in per_model.items()
        },
    }
    safe_write_json(out_dir / "routing_manifest.json", manifest)
    return manifest


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

def make_build_signature(args, as2web_json: Path, index_path: Path,
                         prev_map=None, prev_wc=None):
    """``prev_map`` / ``prev_wc`` are the ALREADY-RESOLVED previous-snapshot
    files (resolved once in main() before the build-cache check), so the
    signature fingerprints exactly the files routing will load -- not a
    re-resolution that could pick a different file if tmp/ changed in between."""
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
            "html_byte_cap": args.html_byte_cap,
            "html_timeout_sec": args.html_timeout_sec,
            "tokens_per_batch_cap": args.tokens_per_batch_cap,
            "prompt_contract": args.prompt_contract,
            "contract_fingerprint": contract_fingerprint(args.prompt_contract),
            "routing_mode": args.routing_mode,
        }
    }
    if args.routing_mode == "versioned":
        payload["params"].update({
            "routing_framing_overhead_tokens": args.routing_framing_overhead_tokens,
            "routing_policy_version": routing.ROUTING_POLICY_VERSION,
            "allow_no_prior": bool(args.allow_no_prior),
            "prev_mapping": file_fingerprint(Path(prev_map)) if prev_map else None,
            "prev_web_class": file_fingerprint(Path(prev_wc)) if prev_wc else None,
        })
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


def extract_response_parts(obj: dict) -> dict:
    """Pull everything the materializer needs from one Batch output line:
    assistant text, an explicit ``refusal`` string (Structured Outputs
    surfaces refusals as a separate field, not schema-compliant JSON),
    ``finish_reason``, token ``usage`` (incl. ``prompt_tokens_details``),
    HTTP ``status_code`` and any ``error``."""
    response = obj.get("response") or {}
    body = response.get("body") or {}
    out = {
        "text": "",
        "refusal": None,
        "finish_reason": None,
        "usage": body.get("usage") or {},
        "status_code": response.get("status_code"),
        "error": obj.get("error"),
    }

    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        ch0 = choices[0] or {}
        msg = ch0.get("message") or {}
        out["finish_reason"] = ch0.get("finish_reason")
        if isinstance(msg.get("refusal"), str) and msg["refusal"].strip():
            out["refusal"] = msg["refusal"].strip()
        out["text"] = flatten_message_content(msg.get("content"))
        return out

    # Responses-API-shaped payloads (kept for forward/back compatibility)
    output = body.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "refusal" and isinstance(item.get("refusal"), str):
                out["refusal"] = item["refusal"].strip()
            for c in item.get("content") or []:
                if isinstance(c, dict):
                    if c.get("type") == "refusal" and isinstance(c.get("refusal"), str):
                        out["refusal"] = c["refusal"].strip()
                    elif isinstance(c.get("text"), str):
                        parts.append(c["text"])
        out["text"] = "".join(parts).strip()

    return out


def extract_response_text_from_batch_line(obj: dict) -> str:
    return extract_response_parts(obj).get("text", "")


# parse-status values recorded per prompt in the materialized metadata
PARSE_OK = "ok"                       # valid JSON, non-empty, all IDs known
PARSE_EMPTY = "empty"                 # no text and no refusal (unanswered)
PARSE_REFUSAL = "refusal"             # model refused; not a "zero categories" result
PARSE_INVALID_JSON = "invalid_json"   # non-empty text that is not the contract JSON
PARSE_LEGACY_NAME_MATCH = "legacy_name_match"  # matched by the pre-202609 name matcher
#: contract JSON was returned but violates the 2026-09 contract: an empty
#: category_ids list, a non-string ID, or an ID outside C001-C097. Under a
#: strict json_schema this should be impossible, so it blocks promotion.
PARSE_CONTRACT_VIOLATION = "contract_violation"


def classify_response(parts: dict, prompt_contract: str | None = None):
    """Map a parsed Batch line (from ``extract_response_parts``) to
    ``(categories, parse_status)`` under the recorded prompt contract.

    - de-duplicates and drops the ``C097`` website-issue sentinel;
    - a ``PARSE_CONTRACT_VIOLATION`` for: an empty ``category_ids`` list, a
      non-string ID, an ID outside the C001-C097 enum, or ``C097`` returned
      alongside any other ID (Website issue must be used alone). The strict
      schema cannot express these (``minItems`` / conditional exclusivity are
      outside the documented strict subset), so they are checked here;
    - a refusal is ``PARSE_REFUSAL`` with no categories -- distinct from an
      empty/zero result;
    When ``prompt_contract`` is recorded, parsing is strict: the current
    contract accepts only category-ID JSON and the legacy contract accepts
    only full category names. ``None`` retains auto-detection solely for old
    artifacts created before mappings recorded their contract.
    """
    if prompt_contract is not None and prompt_contract not in PROMPT_CONTRACTS:
        raise ValueError(f"unknown prompt contract: {prompt_contract!r}")

    if parts.get("refusal"):
        return [], PARSE_REFUSAL

    response_text = parts.get("text") or ""
    if not response_text.strip():
        return [], PARSE_EMPTY

    if prompt_contract != LEGACY_PROMPT_CONTRACT:
        try:
            payload = json.loads(response_text)
        except (TypeError, json.JSONDecodeError):
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("category_ids"), list):
            raw_ids = payload["category_ids"]
            categories = []
            seen = set()
            has_website_issue = False
            violation = not raw_ids  # empty list is a contract violation
            for cid in raw_ids:
                if not isinstance(cid, str):
                    violation = True
                    continue
                if cid in seen:
                    continue
                seen.add(cid)
                if cid == WEBSITE_ISSUE_CATEGORY_ID:
                    has_website_issue = True
                    continue
                name = category_id_to_name_202609.get(cid)
                if name is None:
                    violation = True   # ID outside the enum
                    continue
                categories.append(name)
            # Website issue must be used alone
            if has_website_issue and (categories or len(seen) > 1):
                violation = True
            if violation:
                return categories, PARSE_CONTRACT_VIOLATION
            return categories, PARSE_OK

        if prompt_contract == PROMPT_CONTRACT:
            return [], PARSE_INVALID_JSON

    legacy = []
    for category in taxonomy_list:
        if re.search(r'[^a-zA-Z0-9\s]', category):
            pattern = re.escape(category)
        else:
            pattern = r'\b' + re.escape(category) + r'\b'
        if re.search(pattern, response_text, re.IGNORECASE):
            legacy.append(category)
    return legacy, (PARSE_LEGACY_NAME_MATCH if legacy else PARSE_INVALID_JSON)


def extract_valid_categories(response_text):
    """Back-compatible thin wrapper: categories only, from raw text."""
    cats, _status = classify_response({"text": response_text})
    return cats


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

            parts = extract_response_parts(obj)
            status_code = parts["status_code"]
            error_obj = parts["error"]
            response_text = parts["text"]

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
                "refusal": parts["refusal"],
                "finish_reason": parts["finish_reason"],
                "usage": parts["usage"],
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
        pmeta = final_mapping.get(prompt_id, {})
        cats, parse_status = classify_response({
            "text": response_text,
            "refusal": rec.get("refusal"),
        }, pmeta.get("prompt_contract"))

        asns = rec.get("asns") or final_mapping.get(prompt_id, {}).get("asns", [])
        if not asns:
            continue

        candidate = {
            "prompt_id": prompt_id,
            "categories": cats,
            "parse_status": parse_status,
            "response_text": response_text,
            "refusal": rec.get("refusal"),
            "usage": rec.get("usage") or {},
            "model": pmeta.get("model"),
            "routing": pmeta.get("routing"),
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
# Routed (Luna/Terra) submission and result merge
# =========================================================

def submit_routed_artifacts(client, routed_dir: Path, force: bool = False,
                            hold_lock: bool = True) -> dict:
    """Upload each model stream's part files and create one Batch job per part.
    Crash-safe and resumable at PART granularity: <model>/batch_jobs.json is
    rewritten after every successful submission, and a re-run only submits the
    parts not already recorded there. ``force`` re-submits every part (clears
    the record first).

    Holds routed_dir_lock for the whole upload unless ``hold_lock`` is False,
    which means the caller (emit_routed_artifacts) already holds it across the
    combined write+submit critical section -- re-acquiring here would
    self-deadlock (flock denies a second fd in the same process)."""
    manifest_p = routed_dir / "routing_manifest.json"
    if not manifest_p.exists():
        print(f"❌ {manifest_p} not found — run a --routing-mode versioned build first.")
        return {}
    manifest = safe_read_json(manifest_p, default={})

    lock = None
    if hold_lock:
        try:
            lock = routed_dir_lock(routed_dir)
            lock.__enter__()
        except RuntimeError as e:
            print(f"❌ {e}")
            return {}
    try:
        # final integrity gate: re-hash every part file against the manifest
        # before a single byte is uploaded (protects the reuse path, where the
        # build-time invariants never re-ran)
        try:
            verify_routed_artifacts(routed_dir, manifest)
        except RuntimeError as e:
            print(f"❌ refusing to submit: {e}")
            return {}
        return _submit_routed_artifacts_locked(client, routed_dir, manifest, force)
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _submit_routed_artifacts_locked(client, routed_dir: Path, manifest: dict,
                                    force: bool) -> dict:
    out = {}
    for model in sorted(manifest.get("per_model", {})):
        model_dir = routed_dir / model
        jobs_p = model_dir / "batch_jobs.json"
        if force and jobs_p.exists():
            # Do NOT clear only the in-memory dict: if the first new upload
            # fails, a stale on-disk record would make a later resume skip a
            # part that was never re-submitted. Move it aside first.
            superseded = jobs_p.with_name(f"batch_jobs.superseded-{utc_now_compact()}.json")
            jobs_p.replace(superseded)
            print(f"  {model}: --force-resubmit; previous {jobs_p.name} -> {superseded.name}")
        jobs = {} if force else dict(safe_read_json(jobs_p, default={}) or {})
        parts = sorted(model_dir.glob("batch_input.part*.jsonl"),
                       key=lambda p: int(re.search(r"part(\d+)", p.name).group(1)))
        if not parts:
            print(f"  {model}: no part files to submit.")
            continue
        pending = [p for p in parts if p.name not in jobs]
        if not pending:
            print(f"  {model}: all {len(parts)} part(s) already submitted (in {jobs_p.name}).")
            out[model] = jobs
            continue
        print(f"  {model}: {len(jobs)} part(s) already submitted, {len(pending)} to submit.")
        for part in pending:
            job_id = upload_and_run_batch(client, part)
            if job_id:
                jobs[part.name] = job_id
                safe_write_json(jobs_p, jobs)   # persist immediately, before the next upload
            else:
                print(f"  ❌ {model}/{part.name}: submission failed; "
                      f"{len(jobs)}/{len(parts)} recorded so far. Re-run to resume.")
                break
        out[model] = jobs
        print(f"  {model}: {len(jobs)}/{len(parts)} part(s) recorded -> {jobs_p}")
    return out


def resolve_model_prices(args, models):
    """Return ({model: (input_per_1m, output_per_1m)}, [models_without_a_full_pair]).

    Precedence per model: an exact ``--model-price MODEL:IN:OUT`` entry, the
    built-in Luna/Terra defaults, then the shared
    ``--batch-input-price-per-1m`` + ``--batch-output-price-per-1m`` fallback.
    A lone shared input or output price is never silently zeroed."""
    def _price(spec, raw):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            print(f"❌ {spec}: {raw!r} is not a number")
            sys.exit(2)
        if not math.isfinite(v) or v < 0:
            print(f"❌ {spec}: price must be a finite, non-negative number (got {raw!r})")
            sys.exit(2)
        return v

    explicit = {}
    for spec in (getattr(args, "model_price", None) or []):
        bits = str(spec).split(":")
        if len(bits) != 3 or not bits[0].strip():
            print(f"❌ --model-price {spec!r}: expected MODEL:INPUT_PER_1M:OUTPUT_PER_1M")
            sys.exit(2)
        name = bits[0].strip()
        explicit[name] = (_price(f"--model-price {spec!r} input", bits[1].strip()),
                          _price(f"--model-price {spec!r} output", bits[2].strip()))
    fb_in = args.batch_input_price_per_1m
    fb_out = args.batch_output_price_per_1m
    if fb_in is not None:
        fb_in = _price("--batch-input-price-per-1m", fb_in)
    if fb_out is not None:
        fb_out = _price("--batch-output-price-per-1m", fb_out)
    fallback = (fb_in, fb_out) if (fb_in is not None and fb_out is not None) else None
    resolved, missing = {}, []
    for m in models:
        if m in explicit:
            resolved[m] = explicit[m]
        elif m in DEFAULT_MODEL_PRICES:
            resolved[m] = DEFAULT_MODEL_PRICES[m]
        elif fallback is not None:
            resolved[m] = fallback
        else:
            missing.append(m)
    return resolved, missing


def _assert_submit_complete(routed_dir: Path, manifest: dict) -> None:
    """After a submit attempt, confirm every part file the manifest expects has
    a recorded Batch job id (read back from <model>/batch_jobs.json, the
    crash-safe source of truth). Any gap -> exit 2, so a wrapper / scheduler
    cannot read exit 0 and believe the whole batch was submitted."""
    gaps = []
    total_jobs = 0
    for model in sorted(manifest.get("per_model", {})):
        model_dir = routed_dir / model
        jobs = safe_read_json(model_dir / "batch_jobs.json", default={}) or {}
        total_jobs += sum(1 for v in jobs.values() if v)
        parts = sorted(model_dir.glob("batch_input.part*.jsonl"))
        if not parts:
            gaps.append(f"{model}: no part files on disk")
            continue
        for p in parts:
            if not jobs.get(p.name):
                gaps.append(f"{model}/{p.name}: no Batch job id recorded")
    if gaps:
        print("❌ submission incomplete — do NOT treat this run as a successful submit:")
        for g in gaps:
            print(f"     - {g}")
        print("   Re-run with --submit-batch true (add --force-resubmit only to start "
              "over) to submit the missing parts.")
        sys.exit(2)
    print(f"✅ submission complete: {total_jobs} Batch job(s) recorded across "
          f"{len(manifest.get('per_model', {}))} model stream(s).")


def emit_routed_artifacts(all_task_lines, final_mapping, routed_dir: Path,
                          build_sig: str, args):
    """Single exit point for --routing-mode versioned in preview/all mode
    (fresh build OR build-cache hit): write the isolated per-model artifacts,
    optionally submit, and exit. Never falls through to the single-stream
    submit path, which would mix Luna and Terra into one Batch input file.

    One routed-directory lock is held across BOTH the artifact write/force
    rebuild and the submit, so another process cannot force-rewrite the part
    files in the window between integrity check and upload."""
    lock = routed_dir_lock(routed_dir)
    try:
        lock.__enter__()
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(2)
    try:
        _emit_routed_artifacts_locked(all_task_lines, final_mapping, routed_dir,
                                      build_sig, args)
    finally:
        lock.__exit__(None, None, None)
    sys.exit(0)


def _emit_routed_artifacts_locked(all_task_lines, final_mapping, routed_dir: Path,
                                  build_sig: str, args):
    try:
        routed_manifest = write_routed_artifacts(
            all_task_lines, final_mapping, routed_dir,
            build_sig, args.tokens_per_batch_cap, force=args.force_resubmit,
        )
    except RuntimeError as e:
        # cross-stream invariant failure (fresh build) or on-disk integrity
        # failure (build-cache reuse of tampered/corrupt/hash-less artifacts)
        print(f"❌ {e}")
        sys.exit(2)
    print(f"✅ Routed per-model artifacts under: {routed_dir}")

    models = list(routed_manifest["per_model"].keys())
    prices, missing_price = resolve_model_prices(args, models)

    print(f"    {'model':<16} {'prompts':>8} {'chunks':>7} {'input tok (est)':>16} "
          f"{'compl. budget':>14}   {'$ ceiling (est)':>15}")
    grand_cost = 0.0
    for m, info in routed_manifest["per_model"].items():
        in_tok = info["est_total_tokens"] - info["est_completion_budget_tokens"]
        out_tok = info["est_completion_budget_tokens"]
        line = (f"    {m:<16} {info['prompt_count']:>8} {info['chunk_count']:>7} "
                f"{in_tok:>16,} {out_tok:>14,}   ")
        if m in prices:
            pi, po = prices[m]
            # true upper bound: price every estimated input token at the
            # GPT-5.6 first-write cache premium (1.25x the input rate), so the
            # figure is not undershot when a request writes the cached prefix
            # instead of reading it. Cache reads (~0.1x) and shorter-than-budget
            # completions only move actual spend below this.
            c = in_tok / 1e6 * pi * CACHE_WRITE_PREMIUM + out_tok / 1e6 * po
            grand_cost += c
            line += f"${c:>14,.2f}"
        else:
            line += f"{'(no price)':>15}"
        print(line)
    if prices:
        tail = "" if not missing_price else f"  (excludes {', '.join(missing_price)}: no price)"
        print(f"    {'TOTAL':<16} {'':>8} {'':>7} {'':>16} {'':>14}   ${grand_cost:>14,.2f}"
              + tail)
        print(f"    ($ ceiling: every estimated input token billed at {CACHE_WRITE_PREMIUM:g}x "
              f"the input rate (GPT-5.6 first-write cache premium), plus the entire "
              f"reserved completion budget billed as output. Actual spend is lower -- "
              f"cache reads at ~0.1x and completions shorter than the budget.)")
    if missing_price:
        print(f"    ⚠️ no price for: {', '.join(missing_price)} "
              f"(pass --model-price MODEL:IN_PER_1M:OUT_PER_1M, or the shared "
              f"--batch-input/output-price-per-1m pair). Per-model Batch rates differ "
              f"enough that a shared price is not a valid estimate.")

    if args.submit_batch:
        if missing_price:
            print("❌ --submit-batch true but a cost estimate could not be produced for "
                  f"every model ({', '.join(missing_price)}). The deployment plan "
                  "requires a shown cost before upload. Pass "
                  "--model-price MODEL:INPUT_PER_1M:OUTPUT_PER_1M for each model.")
            sys.exit(2)
        if OpenAI is None or not os.environ.get("OPENAI_API_KEY"):
            print("❌ --submit-batch true but OpenAI client / OPENAI_API_KEY unavailable.")
            sys.exit(1)
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        print(">>> Submitting routed per-model Batch jobs...")
        # this process already holds routed_dir_lock (see emit_routed_artifacts)
        submit_routed_artifacts(client, routed_dir, force=args.force_resubmit,
                                hold_lock=False)
        _assert_submit_complete(routed_dir, routed_manifest)
    else:
        print("   (dry run: pass --submit-batch true to upload the routed streams,")
        print("    then --download-results-only true --routing-mode versioned to materialize.)")


def recover_model_from_task_line(line: str):
    try:
        return (json.loads(line).get("body") or {}).get("model")
    except Exception:
        return None


def estimate_request_line_tokens(obj: dict, framing_overhead: int, default_max_completion: int) -> int:
    """Whole-request token estimate for a built Batch line, recomputed from its
    own messages (o200k over developer + every user content part) + chat
    framing + the reserved completion budget. Used when reusing a batch from
    disk without a stored est_tokens."""
    body = obj.get("body") or {}
    text_tok = 0
    for msg in body.get("messages", []):
        c = msg.get("content")
        if isinstance(c, str):
            text_tok += routing_token_count(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_tok += routing_token_count(part["text"])
    mc = body.get("max_completion_tokens") or default_max_completion
    return text_tok + framing_overhead + int(mc)


def resolve_prev_routing_files(args, tmp_root: Path):
    """Locate the previous snapshot's batch_input_mapping.json and
    batch_input_as2biz_main.json for routing. Resolution order:
      1. explicit --prev-mapping / --prev-web-class (both required together);
      2. ./tmp/<--prev-version-tag>/...;
      3. the newest ./tmp/<tag>/ (tag < current --version-tag) that has both.
    Returns (mapping_or_None, web_class_or_None, source_description,
    explicitly_requested). ``explicitly_requested`` is True when the operator
    named a prior via --prev-* or --prev-version-tag; the caller hard-stops on
    an unusable explicit request rather than silently routing everything to
    Terra."""
    explicit = bool(args.prev_mapping or args.prev_web_class or args.prev_version_tag)

    if args.prev_mapping or args.prev_web_class:
        if not (args.prev_mapping and args.prev_web_class):
            return None, None, "incomplete --prev-mapping/--prev-web-class (need both)", True
        for lbl, pth in (("--prev-mapping", args.prev_mapping),
                         ("--prev-web-class", args.prev_web_class)):
            if not os.path.exists(pth):
                return None, None, f"{lbl} {pth} does not exist", True
        return (args.prev_mapping, args.prev_web_class,
                "explicit --prev-mapping/--prev-web-class", True)

    def _pair(tag_dir: Path):
        m = tag_dir / "batch_input_mapping.json"
        w = tag_dir / "batch_input_as2biz_main.json"
        return (str(m), str(w)) if m.exists() and w.exists() else None

    if args.prev_version_tag:
        p = _pair(tmp_root / args.prev_version_tag)
        if p:
            return p[0], p[1], f"--prev-version-tag {args.prev_version_tag}", True
        return None, None, f"--prev-version-tag {args.prev_version_tag} (files not found)", True

    candidates = []
    for d in sorted(tmp_root.glob("*/")):
        tag = d.name.rstrip("/")
        if tag >= args.version_tag:
            continue
        if _pair(d):
            candidates.append(tag)
    if candidates:
        tag = candidates[-1]
        p = _pair(tmp_root / tag)
        return p[0], p[1], f"auto-discovered ./tmp/{tag}", False
    return None, None, f"no prior snapshot with both files under {tmp_root}", False


def download_and_materialize_routed(client, routed_dir: Path, promote: bool = False) -> dict:
    """Download both model streams' outputs, combine by custom_id, classify
    with the 2026-09 category-ID contract, run completeness/conflict checks,
    summarize cache telemetry, and write a staged ASN-level result. The final
    result is only overwritten from staging when ``promote`` is True and every
    check passed."""
    manifest_p = routed_dir / "routing_manifest.json"
    mapping_p = routed_dir / "routing_mapping.json"
    if not manifest_p.exists() or not mapping_p.exists():
        print(f"❌ routing_manifest.json / routing_mapping.json missing under {routed_dir}")
        return {}
    manifest = safe_read_json(manifest_p, default={})
    final_mapping = safe_read_json(mapping_p, default={})

    PARSE_KEYS = ("ok", "refusal", "empty", "invalid_json",
                  "legacy_name_match", "contract_violation")

    def _new_stat():
        d = {"lines": 0, "cached_tokens": 0, "prompt_tokens": 0, "http_error": 0}
        for k in PARSE_KEYS:
            d[k] = 0
        return d

    combined = {}
    seen_stream = {}
    intra_stream_dupes = []
    per_model_stats = {}
    per_chunk_stats = {}   # "<model>/<part>" -> stat (plan: cache telemetry by model AND chunk)
    error_files = []       # kept raw error files, for the report

    for model in sorted(manifest.get("per_model", {})):
        model_dir = routed_dir / model
        jobs = safe_read_json(model_dir / "batch_jobs.json", default={})
        mst = per_model_stats.setdefault(model, {**_new_stat(), "jobs": len(jobs),
                                                 "completed_jobs": 0})
        raw_dir = model_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for part_name, job_id in jobs.items():
            cst = per_chunk_stats.setdefault(f"{model}/{part_name}",
                                             {**_new_stat(), "job_id": job_id})
            try:
                batch = client.batches.retrieve(job_id)
            except Exception as e:
                print(f"  ⚠️ {model}/{part_name}: retrieve {job_id} failed: {e}")
                continue
            if getattr(batch, "status", None) != "completed":
                print(f"  - {model}/{part_name}: status={getattr(batch, 'status', None)}, skip.")
                continue
            part_stem = part_name[:-6] if part_name.endswith(".jsonl") else part_name
            # persist the error file (partial failures) before the output;
            # filename carries the job id so a --force-resubmit of the same
            # part never overwrites a previous job's raw files
            efid = getattr(batch, "error_file_id", None)
            if efid:
                try:
                    etxt = _read_file_content_text(client.files.content(efid))
                    ep = raw_dir / f"{part_stem}.{job_id}.errors.jsonl"
                    ep.write_text(etxt, encoding="utf-8")
                    error_files.append(str(ep))
                    print(f"  ⚠️ {model}/{part_name}: saved error file {efid} -> {ep.name}")
                except Exception as e:
                    print(f"  ⚠️ {model}/{part_name}: error file {efid} download failed: {e}")
            ofid = getattr(batch, "output_file_id", None)
            if not ofid:
                print(f"  - {model}/{part_name}: completed, no output_file_id.")
                continue
            mst["completed_jobs"] += 1
            try:
                text = _read_file_content_text(client.files.content(ofid))
            except Exception as e:
                print(f"  ❌ {model}/{part_name}: download {ofid} failed: {e}")
                continue
            (raw_dir / f"{part_stem}.{job_id}.output.jsonl").write_text(text, encoding="utf-8")

            for raw in text.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                pid = obj.get("custom_id")
                if not pid:
                    continue
                parts = extract_response_parts(obj)
                prompt_meta = final_mapping.get(pid, {})
                prompt_contract = (prompt_meta.get("prompt_contract")
                                   or manifest.get("prompt_contract"))
                cats, parse_status = classify_response(parts, prompt_contract)
                usage = parts.get("usage") or {}
                cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                ptok = int(usage.get("prompt_tokens") or 0)
                is_http_err = bool(parts.get("status_code")) and int(parts["status_code"]) >= 400
                for st in (mst, cst):
                    st["lines"] += 1
                    st["cached_tokens"] += cached
                    st["prompt_tokens"] += ptok
                    st["http_error"] += int(is_http_err)
                    st[parse_status] = st.get(parse_status, 0) + 1

                rec = {
                    "prompt_id": pid, "model": model, "job_id": job_id,
                    "part": part_name, "categories": cats, "parse_status": parse_status,
                    "refusal": parts.get("refusal"), "finish_reason": parts.get("finish_reason"),
                    "status_code": parts.get("status_code"), "error": parts.get("error"),
                    "usage": usage, "response_text": parts.get("text", ""),
                }

                prior = seen_stream.get(pid)
                if prior is not None and prior != model:
                    raise RuntimeError(
                        f"routed merge: custom_id {pid} present in both {prior} and {model} outputs"
                    )
                if pid in combined:
                    intra_stream_dupes.append(pid)   # do not silently overwrite
                seen_stream[pid] = model
                combined[pid] = rec

    # completeness at prompt level
    routed_pids = set(final_mapping)
    got = set(combined)
    missing = routed_pids - got
    unknown = got - routed_pids

    # completeness + attribution at ASN level: every mapped ASN must have a
    # classified result AND it must come from the model it was routed to.
    asn_meta = {}
    asns_missing = []
    asns_wrong_model = []
    for pid, meta in final_mapping.items():
        rec = combined.get(pid)
        routed_model = meta.get("model")
        for asn in meta.get("asns", []):
            asn = str(asn)
            if rec is None:
                asns_missing.append(asn)
                continue
            if routed_model and rec["model"] != routed_model:
                asns_wrong_model.append(asn)
            cand = {
                "prompt_id": pid, "categories": rec["categories"],
                "parse_status": rec["parse_status"], "model": rec["model"],
                "routed_model": routed_model, "routing": meta.get("routing"),
                "refusal": rec.get("refusal"), "usage": rec.get("usage"),
            }
            old = asn_meta.get(asn)
            if old is None or (pid < old["prompt_id"]):
                asn_meta[asn] = cand

    as2biz_main = {a: m["categories"] for a, m in asn_meta.items()}
    totals = {k: sum(s.get(k, 0) for s in per_model_stats.values()) for k in PARSE_KEYS + ("http_error",)}

    report = {
        "routed_dir": str(routed_dir.resolve()),
        "build_signature": manifest.get("build_signature"),
        "routed_prompts": len(routed_pids),
        "results_received": len(got),
        "total_asns": len(as2biz_main),
        "missing_prompts": sorted(missing)[:50],
        "missing_count": len(missing),
        "unknown_custom_ids": sorted(unknown)[:50],
        "unknown_count": len(unknown),
        "intra_stream_duplicate_custom_ids": sorted(set(intra_stream_dupes))[:50],
        "intra_stream_duplicate_count": len(set(intra_stream_dupes)),
        "asns_without_result": sorted(set(asns_missing))[:50],
        "asns_without_result_count": len(set(asns_missing)),
        "asns_wrong_model": sorted(set(asns_wrong_model))[:50],
        "asns_wrong_model_count": len(set(asns_wrong_model)),
        "totals": totals,
        "error_files": error_files,
        "per_model": per_model_stats,
        "per_chunk": per_chunk_stats,
    }

    blockers = {
        "missing_prompts": report["missing_count"],
        "unknown_custom_ids": report["unknown_count"],
        "intra_stream_duplicates": report["intra_stream_duplicate_count"],
        "asns_without_result": report["asns_without_result_count"],
        "asns_wrong_model": report["asns_wrong_model_count"],
        "http_errors": totals["http_error"],
        "invalid_json": totals["invalid_json"],
        "contract_violation": totals["contract_violation"],
        "empty_responses": totals["empty"],
        "legacy_name_match": totals["legacy_name_match"],
        "refusals": totals["refusal"],
    }
    report["promotion_blockers"] = {k: v for k, v in blockers.items() if v}
    checks_ok = not report["promotion_blockers"]

    staging = routed_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    safe_write_json(staging / "as2biz_main.json", as2biz_main)
    safe_write_json(staging / "as2biz_main_meta.json", asn_meta)
    safe_write_json(staging / "materialize_report.json", report)

    print(f"\n>>> Routed materialize: {report['results_received']}/{report['routed_prompts']} "
          f"prompts, {report['total_asns']} ASNs -> {staging}")
    for m, s in per_model_stats.items():
        pt = s["prompt_tokens"] or 1
        print(f"    {m:<16} ok={s['ok']} refusal={s['refusal']} empty={s['empty']} "
              f"invalid_json={s['invalid_json']} contract_violation={s['contract_violation']} "
              f"legacy={s['legacy_name_match']} http_error={s['http_error']} | "
              f"cache {100.0*s['cached_tokens']/pt:.1f}% ({s['cached_tokens']}/{s['prompt_tokens']})")
    for ck, s in sorted(per_chunk_stats.items()):
        pt = s["prompt_tokens"] or 1
        print(f"      {ck:<24} lines={s['lines']} cache {100.0*s['cached_tokens']/pt:.1f}%")
    if report["promotion_blockers"]:
        print("    promotion blockers: " + ", ".join(
            f"{k}={v}" for k, v in report["promotion_blockers"].items()))

    if promote and checks_ok:
        safe_write_json(routed_dir / "as2biz_main.json", as2biz_main)
        safe_write_json(routed_dir / "as2biz_main_meta.json", asn_meta)
        print(f"✅ Promoted staged result to {routed_dir}/as2biz_main.json")
    elif promote:
        print("❌ Not promoting: see promotion_blockers in staging/materialize_report.json")
    return report


# =========================================================
# WARC worker
# =========================================================

# --- html2text guardrails (2026-09) -------------------------------------
# html2text is pure-Python and has severe performance cliffs on some large or
# structurally pathological captured pages. Two independent guards, both
# configurable and both defaulting ON, keep a single bad page from dominating
# the whole run:
#   1. HTML_BYTE_CAP  — truncate the raw HTML before conversion. The
#      classification prompt only ever uses ~25k tokens (~100 KB) of text,
#      so multi-MB pages carry nothing extra worth parsing.
#   2. HTML_TIMEOUT_SEC — SIGALRM around conv.handle(); on timeout fall back
#      to _cheap_html_to_text() (linear-time <script>/<style>/comment strip
#      + tag strip) so the record still yields usable body text instead of
#      burning CPU for 20 minutes.
# Records processed under either guard differ from a full uncapped
# conversion only for these outliers; the first-screen business text that
# classification relies on is unaffected.

DEFAULT_HTML_BYTE_CAP = 5_000_000     # 0 disables
DEFAULT_HTML_TIMEOUT_SEC = 60         # 0 disables; legit pages convert in <50s,
                                     # anything past this is pathological and
                                     # is better served by the fallback anyway

_ANYTAG_RE = re.compile(r"(?s)<[^>]+>")   # linear: [^>]+ can't backtrack
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


class _HtmlTimeout(Exception):
    pass


def _strip_raw_blocks(s: str) -> str:
    """Remove <script>..</script>, <style>..</style> and <!-- --> blocks by
    linear scan (str.find only) — no regex, so no catastrophic backtracking
    on the multi-MB inline-CSS/JS pages this fallback exists for. Normal
    tags are left intact for _ANYTAG_RE to strip afterwards."""
    low = s.lower()
    out = []
    i, n = 0, len(s)
    while i < n:
        lt = s.find("<", i)
        if lt == -1:
            out.append(s[i:])
            break
        out.append(s[i:lt])
        if low.startswith("<script", lt):
            e = low.find("</script>", lt)
            i = e + 9 if e != -1 else n
        elif low.startswith("<style", lt):
            e = low.find("</style>", lt)
            i = e + 8 if e != -1 else n
        elif s.startswith("<!--", lt):
            e = s.find("-->", lt)
            i = e + 3 if e != -1 else n
        else:
            gt = s.find(">", lt)
            if gt == -1:
                out.append(s[lt:])
                i = n
            else:
                out.append(s[lt:gt + 1])
                i = gt + 1
    return "".join(out)


def _cheap_html_to_text(html_str: str) -> str:
    s = _strip_raw_blocks(html_str)
    s = _ANYTAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def convert_html_capped(conv, payload: bytes, byte_cap: int, timeout_sec: int):
    """Shared HTML->text with a byte cap and a wall-clock timeout on the
    html2text call. Returns the extracted text (falls back to a regex
    tag-strip if html2text exceeds timeout_sec)."""
    if byte_cap and len(payload) > byte_cap:
        payload = payload[:byte_cap]
    html_str = payload.decode("utf-8", errors="replace")

    if timeout_sec and timeout_sec > 0:
        def _on_alarm(signum, frame):
            raise _HtmlTimeout()
        old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(timeout_sec)
        try:
            return conv.handle(html_str).strip()
        except _HtmlTimeout:
            return _cheap_html_to_text(html_str)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:
        return conv.handle(html_str).strip()


def worker_extract_warc(args):
    warc_path_str, target_ids_list, byte_cap, timeout_sec = args
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
                        text = convert_html_capped(conv, payload, byte_cap, timeout_sec)
                    except Exception:
                        text = ""
                    result[rec_id] = text
    except Exception as e:
        raise RuntimeError(f"Error reading {warc_path.name}: {e}") from e

    return result


# =========================================================
# WARC worker — fast path via offset index (2026-09 addition)
# =========================================================
#
# store_index.jsonl (the existing production index) never recorded each
# record's byte offset within its store_*.warc.gz, so worker_extract_warc()
# above has to linearly scan every record in a file to find the handful it
# needs. store_index_offsets.jsonl is a NEW, separate, read-only-built index
# (see build_store_offset_index.py) that adds {warc, record_id, offset,
# gzlen}; when a record's offset is known, we can seek() straight to its
# gzip member instead of scanning. Validated byte-identical against
# worker_extract_warc() on real records before being wired in here.
#
# Some legacy store files were written as one continuous gzip block spanning
# many records instead of one-gzip-member-per-record, so they cannot be
# seek()'d into after the first record. build_store_offset_index.py already
# detects and skips these (they simply have no offset entries beyond record
# 1); worker_extract_warc() above still reads them correctly via full scan,
# so any record missing from the offset index just falls back to it,
# per-file, unchanged from the original behavior.

def _load_offset_index(archives_dir: str):
    """record_id -> (warc_name, offset, gzlen). Returns {} if the index
    hasn't been built yet (safe no-op fallback: everything goes through the
    original full-scan worker_extract_warc)."""
    path = Path(archives_dir) / "store" / "index" / "store_index_offsets.jsonl"
    idx = {}
    if not path.exists():
        return idx
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                idx[d["record_id"]] = (d["warc"], d["offset"], d["gzlen"])
            except Exception:
                continue
    return idx


def worker_extract_records_fast(args):
    """args: (items, byte_cap, timeout_sec) where items is a list of
    (warc_path_str, record_id, offset, gzlen) — may span multiple files;
    grouped/sorted by caller for file-handle locality, but each item is
    independently seekable so order doesn't affect correctness. Reads
    exactly the compressed byte range for each record's own gzip member
    (per-record WARCWriter(gzip=True) output is a concatenation of
    independent gzip members, so this is a valid, self-contained decompress)
    and applies the same guarded html2text conversion as
    worker_extract_warc()."""
    items, byte_cap, timeout_sec = args

    conv = html2text.HTML2Text()
    conv.ignore_links = True
    conv.ignore_images = True
    conv.body_width = 0

    result = {}
    fh_cache = {}
    try:
        for warc_path_str, rec_id, offset, gzlen in items:
            fh = fh_cache.get(warc_path_str)
            if fh is None:
                fh = open(warc_path_str, "rb")
                fh_cache[warc_path_str] = fh
            try:
                fh.seek(offset)
                chunk = fh.read(gzlen) if gzlen and gzlen > 0 else fh.read()
                with gzip.GzipFile(fileobj=io.BytesIO(chunk)) as gz:
                    for record in ArchiveIterator(gz):
                        if record.rec_headers.get_header("WARC-Record-ID") == rec_id:
                            payload = record.content_stream().read()
                            text = convert_html_capped(conv, payload, byte_cap, timeout_sec)
                            result[rec_id] = text
                        break
            except Exception:
                result[rec_id] = ""
    finally:
        for fh in fh_cache.values():
            try:
                fh.close()
            except Exception:
                pass

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
        "--prompt-contract",
        choices=["auto", LEGACY_PROMPT_CONTRACT, PROMPT_CONTRACT],
        default="auto",
        help=("Prompt/output contract. auto selects the legacy full-name contract "
              "before 2026-09-01 and the category-ID contract on/after that date."),
    )
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

    parser.add_argument("--model", default=None,
                        help="Model for every prompt when --routing-mode is off "
                             "(the default). If unset: gpt-5.6-terra for the 2026-09 "
                             "contract, gpt-5.2 for the legacy contract.")
    parser.add_argument("--allow-legacy-model",
                        type=lambda x: str(x).lower() == "true", default=False,
                        help="Permit --routing-mode off with a non-gpt-5.6 --model "
                             "(e.g. gpt-5.2). Off by default: such a run hard-stops so "
                             "the pre-2026-09 model cannot be submitted by accident.")
    parser.add_argument("--max-tokens", type=int, default=25000)
    parser.add_argument("--max-completion-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--tokens-per-batch-cap", type=int, default=DEFAULT_TOKENS_PER_BATCH_CAP)
    parser.add_argument("--html-byte-cap", type=int, default=DEFAULT_HTML_BYTE_CAP,
                        help="Truncate raw HTML to this many bytes before html2text "
                             "(0 disables). Guards against multi-MB pathological pages.")
    parser.add_argument("--html-timeout-sec", type=int, default=DEFAULT_HTML_TIMEOUT_SEC,
                        help="Per-record wall-clock cap on the html2text call (0 disables); "
                             "on timeout, fall back to a linear script/style/tag strip.")

    parser.add_argument("--routing-mode", choices=["off", "versioned"], default=None,
                        help="Default: off — every prompt uses --model (gpt-5.6-terra for "
                             "the 2026-09 contract). Pass 'versioned' to route each unique "
                             "prompt between Luna and Terra by the prior-snapshot policy.")
    parser.add_argument("--prev-version-tag",
                        help="Previous snapshot's version tag (e.g. 2026-03-01); its "
                             "batch_input_mapping.json / batch_input_as2biz_main.json under "
                             "./tmp/<tag>/ are used for routing. If omitted, the newest "
                             "prior snapshot under ./tmp/ is auto-discovered.")
    parser.add_argument("--prev-mapping",
                        help="Explicit path to the previous snapshot's "
                             "batch_input_mapping.json (overrides --prev-version-tag).")
    parser.add_argument("--prev-web-class",
                        help="Explicit path to the previous snapshot's "
                             "batch_input_as2biz_main.json (overrides --prev-version-tag).")
    parser.add_argument("--allow-no-prior",
                        type=lambda x: str(x).lower() == "true", default=False,
                        help="Permit --routing-mode versioned with no usable previous "
                             "snapshot: every website routes to Terra (accuracy-first). "
                             "Off by default so an accidental all-Terra batch hard-stops.")
    parser.add_argument("--model-price", action="append", metavar="MODEL:IN_PER_1M:OUT_PER_1M",
                        help="Override a per-model Batch price in USD per 1M tokens, repeatable: "
                             "MODEL:INPUT_PER_1M:OUTPUT_PER_1M (both finite and >= 0). "
                             "Defaults: gpt-5.6-luna=0.20:1.20 and "
                             "gpt-5.6-terra=2.00:12.00. Check current pricing and "
                             "override these values when rates change.")
    parser.add_argument("--batch-input-price-per-1m", type=float, default=None,
                        help="Shared Batch input $/1M tokens, used only for models without "
                             "a --model-price entry, and only when the matching "
                             "--batch-output-price-per-1m is also given.")
    parser.add_argument("--batch-output-price-per-1m", type=float, default=None,
                        help="Shared Batch output $/1M tokens (see "
                             "--batch-input-price-per-1m).")
    parser.add_argument("--routing-framing-overhead-tokens", type=int,
                        default=DEFAULT_ROUTING_FRAMING_OVERHEAD_TOKENS,
                        help="Per-request chat-framing token overhead the API adds on top "
                             "of the encoded message text, added to the routing "
                             "prompt-length metric so it matches usage.prompt_tokens. "
                             f"Default {DEFAULT_ROUTING_FRAMING_OVERHEAD_TOKENS} was measured "
                             "exactly on the 100-site evaluation (o200k_base); re-measure "
                             "if the request shape or chat template changes.")
    parser.add_argument("--routing-output-dir",
                        help="Directory for isolated per-model routed artifacts "
                             "(default: <batch jsonl dir>/routed/<contract>__<routing version>). "
                             "Only used with --routing-mode versioned.")

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
    parser.add_argument(
        "--promote-routed",
        type=lambda x: str(x).lower() == "true",
        default=False,
        help="Routed download: overwrite the final routed as2biz_main.json from staging "
             "only if completeness/conflict checks pass."
    )

    args = parser.parse_args()

    try:
        selected_contract = resolve_prompt_contract(args.version_tag, args.prompt_contract)
    except ValueError as e:
        parser.error(str(e))
    args.prompt_contract = selected_contract.name
    if args.routing_mode is None:
        args.routing_mode = "off"
    if args.model is None:
        args.model = ("gpt-5.6-terra" if selected_contract.structured_output
                      else "gpt-5.2")
    if args.routing_mode == "versioned" and not selected_contract.supports_versioned_routing:
        parser.error(
            f"--routing-mode versioned requires {PROMPT_CONTRACT}; "
            f"snapshot {args.version_tag!r} selected {selected_contract.name}"
        )
    print(f">>> Prompt contract: {selected_contract.name}")

    if args.output_mode == "local":
        args.mode = "all"
        args.submit_batch = False
        args.routing_mode = "off"   # a local prompt dump is a single-view artifact

    # A non-GPT-5.6 model must be explicitly acknowledged when paired with the
    # 2026-09 category-ID contract. The default (--model unset -> gpt-5.6-terra)
    # passes; this only fires when the user forces an older model. Offline prompt
    # dumps are exempt.
    if (selected_contract.structured_output
            and args.routing_mode == "off" and args.output_mode != "local"
            and not routing.is_gpt_56(args.model) and not args.allow_legacy_model):
        print(f"❌ --routing-mode off with --model {args.model}: this is not a 2026-09 "
              f"production model.\n"
              f"   Leave --model unset for gpt-5.6-terra, pass an explicit gpt-5.6* "
              f"--model, use --routing-mode versioned for Luna/Terra routing, or "
              f"--allow-legacy-model true.")
        sys.exit(2)

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

    version_dir = Path(args.archives_dir) / "versions" / args.version_tag
    index_path = version_dir / "index.jsonl"
    as2web_path = Path(args.as2web_json)

    # Resolve + validate the previous-snapshot files ONCE, here, before the
    # build signature and before the Phase-0 build-cache check. Two reasons:
    #   * the signature must fingerprint exactly the files routing will load
    #     (a second resolution after ~15 min of WARC work could pick a newer
    #     tmp/<tag>/ that landed in between);
    #   * the no-prior hard-stop must run before cache reuse, or an
    #     --allow-no-prior all-Terra build could be re-submitted by a bare
    #     re-run that hits the cache and skips the routing setup entirely.
    prev_map = prev_wc = None
    if args.routing_mode == "versioned" and not args.download_results_only:
        prev_map, prev_wc, prev_src, prev_explicit = resolve_prev_routing_files(
            args, Path("./tmp"))
        if not (prev_map and prev_wc):
            if prev_explicit:
                # An explicitly named prior that cannot be used is always a
                # hard stop -- never silently downgraded to an all-Terra batch,
                # so --allow-no-prior is deliberately NOT offered here.
                print(f"❌ Routing: an explicit previous snapshot was requested but is "
                      f"not usable ({prev_src}). Fix --prev-mapping / --prev-web-class / "
                      f"--prev-version-tag so both files exist and parse.")
                sys.exit(2)
            if not args.allow_no_prior:
                print(f"❌ Routing: no usable previous-snapshot classification "
                      f"({prev_src}); every website would route to Terra -- an expensive "
                      f"all-Terra batch. Specify a prior (--prev-version-tag / "
                      f"--prev-mapping + --prev-web-class), or pass --allow-no-prior true.")
                sys.exit(2)
        else:
            # Both paths resolved -- validate they are readable JSON now, before
            # ~15 min of WARC work, so a corrupt/truncated prior fails fast.
            for lbl, pth in (("previous mapping", prev_map),
                             ("previous website-classification", prev_wc)):
                try:
                    with open(pth, "r", encoding="utf-8") as fh:
                        json.load(fh)
                except Exception as e:
                    hint = ("Fix --prev-mapping / --prev-web-class / --prev-version-tag."
                            if prev_explicit else
                            "Repair or remove that snapshot under ./tmp/, or pass an "
                            "explicit --prev-* pair.")
                    print(f"❌ Routing: {lbl} file is not readable JSON: {pth}\n"
                          f"   ({type(e).__name__}: {e})\n   {hint}")
                    sys.exit(2)

    build_sig, build_payload = make_build_signature(
        args, as2web_path, index_path, prev_map, prev_wc)

    def _routed_dir_for(batch_path: Path) -> Path:
        # Build-time only: includes the build id so a different input set /
        # routing parameter set gets its own directory and never overwrites
        # another run's artifacts.
        if args.routing_output_dir:
            return Path(args.routing_output_dir)
        return (batch_path.parent / "routed"
                / f"{args.prompt_contract}__{routing.ROUTING_POLICY_VERSION}__{build_sig[:12]}")

    def _routed_dir_for_download(batch_path: Path) -> Path:
        # Download-time: never re-derive build_sig (it moves with input file
        # mtimes and the --prev-* args). Prefer an explicit override, then the
        # routed_dir recorded in the base build manifest.
        if args.routing_output_dir:
            return Path(args.routing_output_dir)
        bm = safe_read_json(build_manifest_path(batch_path), default={}) or {}
        rd = bm.get("routed_dir")
        if rd and Path(rd).exists():
            return Path(rd)
        print(f"❌ Cannot locate the routed artifact directory. Pass "
              f"--routing-output-dir explicitly, or ensure "
              f"{build_manifest_path(batch_path).name} records routed_dir "
              f"(re-run the build).")
        sys.exit(1)

    if args.download_results_only:
        if OpenAI is None:
            print("❌ OpenAI library not installed.")
            sys.exit(1)

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("❌ OPENAI_API_KEY not found.")
            sys.exit(1)

        client = OpenAI(api_key=api_key)
        if args.routing_mode == "versioned":
            download_and_materialize_routed(
                client, _routed_dir_for_download(batch_jsonl_path),
                promote=args.promote_routed)
            sys.exit(0)

        if not mapping_path.exists():
            print(f"❌ Mapping file not found: {mapping_path}")
            sys.exit(1)
        download_and_materialize_results(client, batch_jsonl_path, mapping_path)
        sys.exit(0)

    prompt_state = load_prompt_state(state_path)
    chunk_manifest = load_chunk_manifest(cmanifest_path)

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

        final_mapping = safe_read_json(mapping_path, default={})
        with open(batch_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                pid = obj.get("custom_id")
                # real per-task token estimate: the one stored at build time,
                # else recomputed from the request's own messages (o200k +
                # framing + completion) -- never a flat max_tokens guess, which
                # under-counts developer/framing tokens and mis-chunks.
                stored = (final_mapping.get(pid) or {}).get("est_tokens")
                tok = stored if isinstance(stored, int) else estimate_request_line_tokens(
                    obj, args.routing_framing_overhead_tokens, args.max_completion_tokens)
                all_task_lines.append({
                    "line": line,
                    "tokens": tok,
                    "prompt_id": pid,
                    "model": (obj.get("body") or {}).get("model"),
                })

        print(f"Loaded {len(all_task_lines)} tasks from existing batch JSONL.")

        if args.routing_mode == "versioned":
            # never fall through to the single-stream submit path
            emit_routed_artifacts(all_task_lines, final_mapping,
                                  _routed_dir_for(batch_jsonl_path), build_sig, args)

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

        print(">>> Extracting HTML text from store WARCs (offset-index fast path + full-scan fallback)...")

        offset_index = _load_offset_index(args.archives_dir)
        if offset_index:
            print(f"    Loaded offset index: {len(offset_index)} records "
                  f"({Path(args.archives_dir) / 'store' / 'index' / 'store_index_offsets.jsonl'})")
        else:
            print("    ⚠️ No store_index_offsets.jsonl found — falling back to full-scan for everything "
                  "(run build_store_offset_index.py once to speed this up).")

        fast_items = []   # (warc_path_str, record_id, offset, gzlen)
        slow_tasks = []   # (warc_path_str, [record_id, ...]) — unchanged full-scan path
        n_missing_warc = 0
        n_fast = 0
        n_slow = 0
        for warc_name, target_rids in warc_to_rec_ids.items():
            warc_path = store_warc_base / warc_name
            if not warc_path.exists():
                print(f"⚠️ Missing store WARC: {warc_name}")
                n_missing_warc += 1
                continue
            missing_rids = []
            for rid in target_rids:
                hit = offset_index.get(rid)
                if hit and hit[0] == warc_name:
                    fast_items.append((str(warc_path), rid, hit[1], hit[2]))
                    n_fast += 1
                else:
                    missing_rids.append(rid)
            if missing_rids:
                slow_tasks.append((str(warc_path), missing_rids))
                n_slow += len(missing_rids)

        print(f"    {n_fast} records via fast seek, {n_slow} records via full-scan fallback "
              f"({n_slow/(n_fast+n_slow):.2%} fallback)" if (n_fast + n_slow) else "    no records to extract")

        # Evenly chunk the fast (seekable) records across workers — sorted by
        # warc path first so each chunk tends to touch fewer distinct files
        # (file handles are cached per-chunk in worker_extract_records_fast).
        fast_items.sort(key=lambda t: t[0])
        n_fast_chunks = max(1, min(args.workers, (len(fast_items) // 200) + 1)) if fast_items else 0
        fast_chunks = []
        if fast_items:
            chunk_size = (len(fast_items) + n_fast_chunks - 1) // n_fast_chunks
            fast_chunks = [fast_items[i:i + chunk_size] for i in range(0, len(fast_items), chunk_size)]

        html_byte_cap = args.html_byte_cap
        html_timeout_sec = args.html_timeout_sec
        print(f"    html2text guards: byte_cap={html_byte_cap or 'off'}, "
              f"timeout_sec={html_timeout_sec or 'off'}")

        total_units = len(fast_chunks) + len(slow_tasks)
        if total_units:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                future_to_label = {}
                for chunk in fast_chunks:
                    fut = executor.submit(worker_extract_records_fast,
                                          (chunk, html_byte_cap, html_timeout_sec))
                    future_to_label[fut] = f"fast-chunk({len(chunk)} recs)"
                for warc_path_str, rids in slow_tasks:
                    fut = executor.submit(worker_extract_warc,
                                          (warc_path_str, rids, html_byte_cap, html_timeout_sec))
                    future_to_label[fut] = f"fallback:{Path(warc_path_str).name}"

                for future in tqdm(as_completed(future_to_label), total=len(future_to_label),
                                    unit="unit", desc="Extract"):
                    label = future_to_label[future]
                    try:
                        partial = future.result()
                        rec_text.update(partial)
                    except Exception as e:
                        tqdm.write(f"❌ Error in worker for {label}: {e}")
        else:
            print("⚠️ No WARC tasks to process. rec_text will be empty.")

        print(">>> Building per-ASN prompts (landing first, then subpages, with dedup)...")

        # --- routing setup (2026-09; default) ---
        routing_mode = args.routing_mode
        prev_index = None
        routing_reason_counts = Counter()
        routing_model_counts = Counter()
        if routing_mode == "versioned":
            # prev_map / prev_wc were resolved and validated once, before the
            # build-cache check (see main() near make_build_signature).
            if prev_map and prev_wc:
                prev_index = routing_features.PrevWebsiteClassIndex.load(
                    prev_map, prev_wc, set(taxonomy_list), WEBSITE_ISSUE_CATEGORY_NAME)
                print(">>> Routing: previous-snapshot classification loaded")
                print("    " + prev_index.stats.summary().replace("\n", "\n    "))
            else:
                print("⚠️ Routing: --allow-no-prior with no usable prior. Every website "
                      "routes to Terra (accuracy-first); the Luna branch is disabled "
                      "this run.")
            if not routing.is_gpt_56(args.model):
                print(f"    (note: --model {args.model} is ignored under --routing-mode "
                      f"versioned; each prompt's model comes from the router)")

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
                    f"{selected_contract.user_template}\n\n"
                    f"{selected_contract.taxonomy}\n\n"
                    f"{selected_contract.descriptions}\n\n"
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

            if prompt_hash not in prompt_hash_to_task:
                routing_meta = None
                # whole-request token estimate (o200k + developer + stable
                # prefix + body + chat framing) -- matches usage.prompt_tokens,
                # unlike a user-content-only count. Used for both routing and
                # the batch token-cap chunker.
                if selected_contract.structured_output:
                    complete_prompt_tokens = routing_features.complete_prompt_token_count(
                        selected_contract.developer_prompt,
                        base_prompt,
                        aggregated_body,
                        routing_token_count,
                        args.routing_framing_overhead_tokens,
                    )
                else:
                    complete_prompt_tokens = count_tokens(
                        selected_contract.developer_prompt + "\n" + full_content,
                        args.model,
                    )
                if routing_mode == "versioned":
                    if prev_index is not None:
                        prev = prev_index.lookup([rep_url])
                        has_prev, prior_ct = prev.has_previous_classification, prev.prior_category_count
                        ambiguous, prev_key, prev_status = prev.ambiguous, prev.matched_key, prev.status
                    else:
                        has_prev, prior_ct, ambiguous = False, None, False
                        prev_key, prev_status = None, "no_prev_index"
                    decision = routing.route_prompt(
                        prior_ct, complete_prompt_tokens, has_prev,
                        ambiguous_previous_classification=ambiguous,
                    )
                    task_model = decision.model
                    routing_meta = decision.as_dict()
                    routing_meta["prev_matched_website_key"] = prev_key
                    routing_meta["prev_status"] = prev_status
                    routing_reason_counts[decision.reason] += 1
                    routing_model_counts[decision.model] += 1
                else:
                    task_model = args.model

                task = build_classification_task(
                    custom_id=prompt_id,
                    model=task_model,
                    developer_prompt=selected_contract.developer_prompt,
                    stable_user_prefix=base_prompt,
                    variable_body=aggregated_body,
                    temperature=args.temperature,
                    max_completion_tokens=args.max_completion_tokens,
                    prompt_contract=selected_contract.name,
                )
                est_tokens = complete_prompt_tokens + args.max_completion_tokens

                prompt_hash_to_task[prompt_hash] = {
                    "line": json.dumps(task, ensure_ascii=False),
                    "tokens": est_tokens,
                    "prompt_id": prompt_id,
                    "prompt_hash": prompt_hash,
                    "model": task_model,
                }

                prompt_id_to_meta[prompt_id] = {
                    "prompt_hash": prompt_hash,
                    "build_id": build_id,
                    "prompt_contract": selected_contract.name,
                    "model": task_model,
                    "max_tokens": args.max_tokens,
                    "max_completion_tokens": args.max_completion_tokens,
                    "complete_prompt_tokens": complete_prompt_tokens,
                    "est_tokens": est_tokens,
                    "asns": set(),
                    "landing_urls": [],
                    "included_urls": [],
                    "included_record_ids": [],
                    "created_at": utc_now_iso(),
                }
                if routing_meta is not None:
                    prompt_id_to_meta[prompt_id]["routing"] = routing_meta

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

        if routing_mode == "versioned":
            total_routed = sum(routing_model_counts.values())
            print(f">>> Routing ({routing.ROUTING_POLICY_VERSION}): {total_routed} prompts routed")
            for m in (routing.MODEL_LUNA, routing.MODEL_TERRA):
                c = routing_model_counts.get(m, 0)
                pct = (100.0 * c / total_routed) if total_routed else 0.0
                print(f"    {m:<16} {c:>7}  ({pct:.1f}%)")
            for reason, c in routing_reason_counts.most_common():
                print(f"    reason {reason:<32} {c:>7}")
            if total_routed != len(all_task_lines):
                print(f"    ⚠️ routed count {total_routed} != unique prompt count "
                      f"{len(all_task_lines)}")

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
                        "user_prompt": flatten_message_content(messages[1]["content"]),
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
        if routing_mode == "versioned":
            manifest["routed_dir"] = str(_routed_dir_for(batch_jsonl_path).resolve())
        safe_write_json(manifest_path, manifest)
        print(f"✅ Build manifest saved to: {manifest_path}")

        if routing_mode == "versioned":
            emit_routed_artifacts(all_task_lines, final_mapping,
                                  _routed_dir_for(batch_jsonl_path), build_sig, args)

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
