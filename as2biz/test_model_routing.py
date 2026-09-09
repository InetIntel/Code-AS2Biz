"""Standalone tests for as2biz_model_routing (run: python3 as2biz/test_model_routing.py).

No pytest dependency; plain asserts. Covers every branch of the adopted
routing rule, both threshold boundaries, and the output invariants.
"""

import as2biz_model_routing as r


def _check(prior, tokens, has_prev, want_model, want_reason):
    d = r.route_prompt(prior, tokens, has_prev)
    assert d.model == want_model, (prior, tokens, has_prev, d.model, "!=", want_model)
    assert d.reason == want_reason, (prior, tokens, has_prev, d.reason, "!=", want_reason)
    assert d.model in (r.MODEL_LUNA, r.MODEL_TERRA)
    assert d.policy_version == r.ROUTING_POLICY_VERSION
    # round-trips cleanly for mapping metadata
    assert d.as_dict()["model"] == d.model
    return d


def test_no_previous_classification_defaults_terra():
    _check(None, 100, False, r.MODEL_TERRA, r.REASON_MISSING_PREVIOUS_CLASSIFICATION)
    _check(None, 999999, False, r.MODEL_TERRA, r.REASON_MISSING_PREVIOUS_CLASSIFICATION)
    # has_previous_classification=False wins even if a count is (wrongly) passed
    _check(7, 100, False, r.MODEL_TERRA, r.REASON_MISSING_PREVIOUS_CLASSIFICATION)
    # prior=None with has_previous=True is also treated as missing
    _check(None, 100, True, r.MODEL_TERRA, r.REASON_MISSING_PREVIOUS_CLASSIFICATION)


def test_ambiguous_previous_classification_forces_terra():
    # ambiguous wins over everything: short prompt, low prior count, etc.
    d = r.route_prompt(2, 100, True, ambiguous_previous_classification=True)
    assert d.model == r.MODEL_TERRA
    assert d.reason == r.REASON_AMBIGUOUS_PREVIOUS_CLASSIFICATION
    assert d.prior_category_count is None
    # also wins over prior_categories_ge_5
    d = r.route_prompt(9, 100, True, ambiguous_previous_classification=True)
    assert d.reason == r.REASON_AMBIGUOUS_PREVIOUS_CLASSIFICATION
    # and over a plain missing signal
    d = r.route_prompt(None, 5000, False, ambiguous_previous_classification=True)
    assert d.reason == r.REASON_AMBIGUOUS_PREVIOUS_CLASSIFICATION


def test_prior_categories_ge_5_forces_terra():
    _check(5, 10, True, r.MODEL_TERRA, r.REASON_PRIOR_CATEGORIES_GE_5)
    _check(5, 999999, True, r.MODEL_TERRA, r.REASON_PRIOR_CATEGORIES_GE_5)
    _check(20, 1, True, r.MODEL_TERRA, r.REASON_PRIOR_CATEGORIES_GE_5)


def test_short_prompt_routes_luna():
    _check(0, 6000, True, r.MODEL_LUNA, r.REASON_SHORT_PROMPT_LE_6000)
    _check(4, 5999, True, r.MODEL_LUNA, r.REASON_SHORT_PROMPT_LE_6000)
    _check(0, 0, True, r.MODEL_LUNA, r.REASON_SHORT_PROMPT_LE_6000)


def test_long_prompt_routes_terra():
    _check(0, 6001, True, r.MODEL_TERRA, r.REASON_LONG_PROMPT_GT_6000)
    _check(4, 12000, True, r.MODEL_TERRA, r.REASON_LONG_PROMPT_GT_6000)


def test_threshold_boundaries_exact():
    # 6000 is "at most 6000" -> Luna; 6001 -> Terra
    assert r.route_prompt(1, 6000, True).model == r.MODEL_LUNA
    assert r.route_prompt(1, 6001, True).model == r.MODEL_TERRA
    # 4 prior categories is below threshold (token rule applies); 5 forces Terra
    assert r.route_prompt(4, 100, True).model == r.MODEL_LUNA
    assert r.route_prompt(5, 100, True).model == r.MODEL_TERRA


def test_prior_count_checked_before_tokens():
    # >=5 prior categories wins even for a short prompt
    d = r.route_prompt(6, 10, True)
    assert d.model == r.MODEL_TERRA
    assert d.reason == r.REASON_PRIOR_CATEGORIES_GE_5


def test_invalid_inputs_raise():
    for bad in (-1, -100):
        try:
            r.route_prompt(1, bad, True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for tokens={bad}")
    try:
        r.route_prompt(-1, 100, True)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for prior_category_count=-1")


def test_is_gpt_56():
    assert r.is_gpt_56("gpt-5.6")
    assert r.is_gpt_56("gpt-5.6-terra")
    assert r.is_gpt_56("gpt-5.6-luna")
    assert not r.is_gpt_56("gpt-5.2")
    assert not r.is_gpt_56("gpt-5.6.1")  # not a hyphen variant


def test_synthetic_distribution_shape():
    """Not the real 100-site regression (needs data), but a sanity check that
    the rule can produce a mixed split and that Luna only appears for
    short-prompt, low-prior-count, has-previous cases."""
    luna = terra = 0
    for prior in range(0, 8):
        for tokens in (500, 3000, 6000, 6001, 9000, 20000):
            for has_prev in (True, False):
                d = r.route_prompt(prior if has_prev else None, tokens, has_prev)
                if d.model == r.MODEL_LUNA:
                    luna += 1
                    assert has_prev and prior < 5 and tokens <= 6000
                    assert d.reason == r.REASON_SHORT_PROMPT_LE_6000
                else:
                    terra += 1
    assert luna > 0 and terra > 0
    assert luna + terra == 8 * 6 * 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} routing tests passed")
