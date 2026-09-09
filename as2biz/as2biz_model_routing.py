"""AS2Biz 2026-09 model-routing policy.

Pure, dependency-free routing decision for the website-classification batch.
One decision is made per unique website prompt, before ASN fan-out, so that
every ASN mapped to a prompt receives the same model and the same normalized
result.

This is an optional path: the batch defaults to ``--routing-mode off`` with a
single model (GPT-5.6 Terra), and ``--routing-mode versioned`` opts into the
rule below. GPT-5.6 Luna and GPT-5.6 Terra were adjudicated on a fixed
100-website set; Terra was the accuracy-first choice and Luna was reserved for
low-complexity prompts.

Adopted rule (accuracy-first production policy):

1. If the previous snapshot has at least five *direct website* business
   categories for this website, use Terra.
2. Otherwise, if the complete prompt is at most 6,000 tokens, use Luna.
3. Otherwise, use Terra.
4. If there is no usable previous-snapshot classification, use Terra.

Prompt length alone was a weak predictor in evaluation; prior
business-category count is the stronger complexity signal, which is why it is
checked first.
"""

from __future__ import annotations

from dataclasses import dataclass


# --- identifiers ----------------------------------------------------------

MODEL_TERRA = "gpt-5.6-terra"
MODEL_LUNA = "gpt-5.6-luna"

#: Bump whenever the thresholds, ordering, or model constants below change.
#: Recorded alongside every routed prompt and in the build signature.
ROUTING_POLICY_VERSION = "2026-09-routing-v1"

#: Complete-prompt token threshold for the Luna branch. "Complete prompt" means
#: developer instructions + user template + taxonomy + descriptions + message
#: framing + included landing/subpage text -- NOT the builder's user-only
#: ``full_content`` estimate. See the deployment plan's "Prompt-length metric".
PROMPT_TOKEN_THRESHOLD = 6000

#: Minimum prior direct-website business-category count that forces Terra.
PRIOR_CATEGORY_TERRA_THRESHOLD = 5


# --- reason codes -------------------------------------------------------------

REASON_PRIOR_CATEGORIES_GE_5 = "prior_categories_ge_5"
REASON_SHORT_PROMPT_LE_6000 = "short_prompt_le_6000"
REASON_LONG_PROMPT_GT_6000 = "long_prompt_gt_6000"
REASON_MISSING_PREVIOUS_CLASSIFICATION = "missing_previous_classification"
#: The previous snapshot has a classification for this website but it is not a
#: usable complexity signal -- conflicting category sets across the prev
#: prompts that map to this site. Accuracy-first: route Terra.
REASON_AMBIGUOUS_PREVIOUS_CLASSIFICATION = "ambiguous_previous_classification"


@dataclass(frozen=True)
class RoutingDecision:
    model: str
    reason: str
    policy_version: str
    prior_category_count: int | None
    complete_prompt_tokens: int
    has_previous_classification: bool

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "routing_reason": self.reason,
            "routing_policy_version": self.policy_version,
            "prior_business_category_count": self.prior_category_count,
            "complete_prompt_tokens": self.complete_prompt_tokens,
            "has_previous_classification": self.has_previous_classification,
        }


def route_prompt(
    prior_category_count: int | None,
    complete_prompt_tokens: int,
    has_previous_classification: bool,
    ambiguous_previous_classification: bool = False,
) -> RoutingDecision:
    """Decide which model a single unique website prompt should be sent to.

    Parameters
    ----------
    prior_category_count:
        Number of distinct *direct website* business categories the previous
        snapshot assigned to this website. Must exclude inherited-from-ASN
        categories, duplicate labels, and the ``Website issue`` sentinel. Pass
        ``None`` when there is no usable previous classification (equivalently,
        set ``has_previous_classification=False``).
    complete_prompt_tokens:
        Token count of the *complete* request prompt (see
        ``PROMPT_TOKEN_THRESHOLD``), measured with the project's canonical
        GPT-5.6 tokenization method.
    has_previous_classification:
        Whether a *usable* previous-snapshot direct-website classification
        exists: one consistent, non-empty prior business-category set. When
        ``False`` (no match, or a prior of only ``Website issue`` / no business
        category), ``prior_category_count`` is ignored and the prompt routes
        Terra.
    ambiguous_previous_classification:
        The previous snapshot classified this website but the prompts that map
        to it disagree on the category set. Accuracy-first: route Terra with a
        distinct reason. Takes precedence over the checks below.
    """
    if complete_prompt_tokens < 0:
        raise ValueError("complete_prompt_tokens must be non-negative")

    if ambiguous_previous_classification:
        return RoutingDecision(
            model=MODEL_TERRA,
            reason=REASON_AMBIGUOUS_PREVIOUS_CLASSIFICATION,
            policy_version=ROUTING_POLICY_VERSION,
            prior_category_count=None,
            complete_prompt_tokens=complete_prompt_tokens,
            has_previous_classification=False,
        )

    if not has_previous_classification or prior_category_count is None:
        return RoutingDecision(
            model=MODEL_TERRA,
            reason=REASON_MISSING_PREVIOUS_CLASSIFICATION,
            policy_version=ROUTING_POLICY_VERSION,
            prior_category_count=None,
            complete_prompt_tokens=complete_prompt_tokens,
            has_previous_classification=False,
        )

    if prior_category_count < 0:
        raise ValueError("prior_category_count must be non-negative or None")

    if prior_category_count >= PRIOR_CATEGORY_TERRA_THRESHOLD:
        reason = REASON_PRIOR_CATEGORIES_GE_5
        model = MODEL_TERRA
    elif complete_prompt_tokens <= PROMPT_TOKEN_THRESHOLD:
        reason = REASON_SHORT_PROMPT_LE_6000
        model = MODEL_LUNA
    else:
        reason = REASON_LONG_PROMPT_GT_6000
        model = MODEL_TERRA

    return RoutingDecision(
        model=model,
        reason=reason,
        policy_version=ROUTING_POLICY_VERSION,
        prior_category_count=prior_category_count,
        complete_prompt_tokens=complete_prompt_tokens,
        has_previous_classification=True,
    )


def is_gpt_56(model: str) -> bool:
    """True for any GPT-5.6 variant (Luna, Terra, or a bare ``gpt-5.6``)."""
    return model == "gpt-5.6" or model.startswith("gpt-5.6-")
