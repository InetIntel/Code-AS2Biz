"""Routing features for the AS2Biz 2026-09 model-routing policy.

Two inputs feed ``as2biz_model_routing.route_prompt``:

* ``prior_category_count`` -- the number of distinct *direct website* business
  categories the previous snapshot assigned to this website. Provided here by
  ``PrevWebsiteClassIndex``, built by joining the previous snapshot's
  prompt->ASN mapping (``batch_input_mapping.json``) with its direct
  website-classification result (``batch_input_as2biz_main.json``) on a
  normalized website key. The same two files are produced by this generation,
  so the next generation can run the identical join.

* ``complete_prompt_tokens`` -- see ``complete_prompt_token_count``; the token
  count of the whole request, not the builder's user-only estimate.

Pure module: no tiktoken / network / batch-script imports. The caller passes a
tokenizer callable in.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse


# --- website key -----------------------------------------------------------

_SKIP_SCHEMES = ("chrome-error:", "about:", "data:", "file:", "javascript:")


def website_key(url: str) -> str | None:
    """Normalize a landing URL to a stable cross-snapshot website identity:
    lowercased host, leading ``www.`` removed, port removed. Returns ``None``
    for browser-error / non-web URLs that cannot identify a site."""
    if not url:
        return None
    u = url.strip()
    low = u.lower()
    if any(low.startswith(s) for s in _SKIP_SCHEMES):
        return None
    if "://" not in u:
        u = "http://" + u
    host = urlparse(u).netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    host = host.strip(".")
    return host or None


def website_keys(landing_urls) -> set[str]:
    out = set()
    for u in landing_urls or []:
        k = website_key(u)
        if k:
            out.add(k)
    return out


# --- previous-snapshot direct website classification ----------------------

@dataclass
class PrevLoadStats:
    prev_prompts: int = 0
    prev_prompts_with_categories: int = 0
    website_keys: int = 0
    keys_from_multiple_prompts: int = 0
    ambiguous_keys: int = 0
    zero_category_keys: int = 0
    dropped_prompts_no_key: int = 0
    invalid_category_labels: Counter = field(default_factory=Counter)
    category_count_distribution: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        lines = [
            f"prev prompts:                     {self.prev_prompts}",
            f"  with >=1 direct category:       {self.prev_prompts_with_categories}",
            f"  dropped (no usable landing key):{self.dropped_prompts_no_key}",
            f"distinct website keys indexed:    {self.website_keys}",
            f"  seen in >1 prev prompt:         {self.keys_from_multiple_prompts}",
            f"  ambiguous (conflicting prior sets) -> Terra: {self.ambiguous_keys}",
            f"  zero business categories -> Terra:           {self.zero_category_keys}",
        ]
        if self.invalid_category_labels:
            top = ", ".join(f"{k!r}x{v}" for k, v in self.invalid_category_labels.most_common(5))
            lines.append(f"invalid category labels in prev result: {top}")
        dist = ", ".join(f"{n}:{c}" for n, c in sorted(self.category_count_distribution.items()))
        lines.append(f"prior-category-count distribution (status=ok keys): {dist}")
        return "\n".join(lines)


# status values on a PrevLookup / index entry
PREV_OK = "ok"                     # one consistent non-empty prior category set
PREV_MISSING = "missing"           # no website key matched
PREV_AMBIGUOUS = "ambiguous"       # >1 distinct non-empty prior set for the site
PREV_ZERO_CATEGORIES = "zero_categories"  # matched, but no prior *business* category


@dataclass(frozen=True)
class PrevLookup:
    status: str
    matched_key: str | None
    categories: tuple[str, ...]

    @property
    def has_previous_classification(self) -> bool:
        """True only for a single, consistent, non-empty prior classification.
        Ambiguous and zero-category priors are NOT usable signals -- under the
        accuracy-first policy the caller routes them to Terra."""
        return self.status == PREV_OK

    @property
    def prior_category_count(self) -> int | None:
        return len(self.categories) if self.status == PREV_OK else None

    @property
    def ambiguous(self) -> bool:
        return self.status == PREV_AMBIGUOUS


class PrevWebsiteClassIndex:
    """website_key -> (status, sorted tuple of distinct prior *business*
    categories with ``Website issue`` excluded)."""

    def __init__(self, key_to_entry: dict, stats: PrevLoadStats):
        self._idx = key_to_entry  # key -> (status, categories tuple)
        self.stats = stats

    def lookup(self, landing_urls) -> PrevLookup:
        keys = website_keys(landing_urls)
        hits = [(k, self._idx[k]) for k in keys if k in self._idx]
        if not hits:
            return PrevLookup(PREV_MISSING, None, ())
        if len(hits) == 1:
            k, (status, cats) = hits[0]
            return PrevLookup(status, k, cats)

        # current prompt spans several prev website keys: usable only if every
        # matched key is status=ok with the *same* category set.
        mk = sorted(k for k, _ in hits)[0]
        ok_sets = {frozenset(cats) for _, (st, cats) in hits if st == PREV_OK}
        if any(st == PREV_AMBIGUOUS for _, (st, _c) in hits) or len(ok_sets) > 1:
            merged = tuple(sorted({c for _, (_st, cats) in hits for c in cats}))
            return PrevLookup(PREV_AMBIGUOUS, mk, merged)
        if len(ok_sets) == 1:
            return PrevLookup(PREV_OK, mk, tuple(sorted(next(iter(ok_sets)))))
        return PrevLookup(PREV_ZERO_CATEGORIES, mk, ())

    def __len__(self):
        return len(self._idx)

    @classmethod
    def load(
        cls,
        prev_mapping_path: str,
        prev_web_class_path: str,
        valid_category_names: set[str],
        website_issue_name: str,
    ) -> "PrevWebsiteClassIndex":
        with open(prev_mapping_path, "r", encoding="utf-8") as f:
            prev_mapping = json.load(f)
        with open(prev_web_class_path, "r", encoding="utf-8") as f:
            prev_web_class = json.load(f)

        stats = PrevLoadStats()
        acc: dict[str, dict] = {}  # key -> {"nonempty_sets": set(frozenset), "prompts": int}

        for _pid, meta in prev_mapping.items():
            stats.prev_prompts += 1
            keys = website_keys(meta.get("landing_urls"))
            if not keys:
                stats.dropped_prompts_no_key += 1
                continue

            cats: set[str] = set()
            for asn in meta.get("asns", []) or []:
                for c in prev_web_class.get(str(asn), []) or []:
                    if not isinstance(c, str):
                        continue
                    if c == website_issue_name:
                        continue
                    if c not in valid_category_names:
                        stats.invalid_category_labels[c] += 1
                        continue
                    cats.add(c)
            if cats:
                stats.prev_prompts_with_categories += 1

            for k in keys:
                slot = acc.setdefault(k, {"nonempty_sets": set(), "prompts": 0})
                slot["prompts"] += 1
                if cats:
                    slot["nonempty_sets"].add(frozenset(cats))

        key_to_entry: dict = {}
        for k, slot in acc.items():
            nonempty = slot["nonempty_sets"]
            if slot["prompts"] > 1:
                stats.keys_from_multiple_prompts += 1
            if len(nonempty) > 1:
                # Conflicting prior classifications for one site: not a usable
                # signal. Keep the union only for inspection; route Terra.
                status = PREV_AMBIGUOUS
                cats_tuple = tuple(sorted(set().union(*nonempty)))
                stats.ambiguous_keys += 1
            elif len(nonempty) == 1:
                status = PREV_OK
                cats_tuple = tuple(sorted(next(iter(nonempty))))
                stats.category_count_distribution[len(cats_tuple)] += 1
            else:
                status = PREV_ZERO_CATEGORIES
                cats_tuple = ()
                stats.zero_category_keys += 1
            key_to_entry[k] = (status, cats_tuple)

        stats.website_keys = len(key_to_entry)
        return cls(key_to_entry, stats)


# --- complete-prompt token metric ---------------------------------------------

def complete_prompt_token_count(
    developer_prompt: str,
    stable_user_prefix: str,
    variable_body: str,
    token_counter,
    framing_overhead_tokens: int,
) -> int:
    """Token count of the whole request, per the deployment plan's
    "Prompt-length metric": developer instructions + user template + taxonomy
    + descriptions + message framing + included landing/subpage text.

    ``token_counter`` is a callable ``str -> int`` -- for GPT-5.6 this MUST be
    an o200k_base counter (cl100k_base mis-tokenizes non-Latin scripts by
    thousands of tokens). ``framing_overhead_tokens`` is the per-request
    chat-framing overhead the API adds on top of the encoded message text;
    with o200k_base it was an exact constant (341) across all 100 evaluation
    requests, so this term makes the metric match ``usage.prompt_tokens``
    rather than being a loose allowance.
    """
    return (
        token_counter(developer_prompt)
        + token_counter(stable_user_prefix)
        + token_counter(variable_body)
        + framing_overhead_tokens
    )
