"""Standalone tests for as2biz_routing_features (run: python3 as2biz/test_routing_features.py)."""

import json
import os
import tempfile

import as2biz_routing_features as rf


def test_website_key_normalization():
    assert rf.website_key("https://www.BMW.com/") == "bmw.com"
    assert rf.website_key("http://bmw.com") == "bmw.com"
    assert rf.website_key("https://shop.bmw.com:8443/x/y?z=1") == "shop.bmw.com"
    assert rf.website_key("bmw.com/path") == "bmw.com"
    assert rf.website_key("https://user:pw@bmw.com/") == "bmw.com"
    assert rf.website_key("www.example.co.uk") == "example.co.uk"
    for bad in ("", "   ", "chrome-error://chromewebdata/", "about:blank",
                "data:text/html,x", "javascript:void(0)"):
        assert rf.website_key(bad) is None, bad


def test_website_keys_set():
    assert rf.website_keys(["https://www.a.com/", "http://a.com", "chrome-error://x"]) == {"a.com"}
    assert rf.website_keys([]) == set()
    assert rf.website_keys(None) == set()


def _tmp_json(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    return path


def test_prev_index_join_and_lookup():
    ISSUE = "Website issue - Cannot determine categories"
    valid = {"Cat A", "Cat B", "Cat C", "Cat D", "Cat E", "Cat F"}
    mapping = {
        "p1": {"asns": ["1", "2"], "landing_urls": ["https://www.foo.com/"]},
        "p2": {"asns": ["3"], "landing_urls": ["http://bar.com"]},
        "p3": {"asns": ["9"], "landing_urls": ["chrome-error://chromewebdata/"]},  # dropped
        "p4": {"asns": ["4"], "landing_urls": ["https://baz.com"]},   # baz has only Website issue
        "p5": {"asns": ["5"], "landing_urls": ["https://foo.com/other"]},  # same key as p1, different set
        "p6": {"asns": ["6"], "landing_urls": ["https://qux.com"]},   # one non-empty set
        "p7": {"asns": ["7"], "landing_urls": ["https://qux.com/x"]}, # empty set: not a conflict
    }
    web_class = {
        "1": ["Cat A", "Cat B"],
        "2": ["Cat A", "Cat B"],           # agrees with asn 1
        "3": ["Cat A", "Cat B", "Cat C", "Cat D", "Cat E"],  # 5 -> Terra-worthy
        "4": [ISSUE],                       # website issue only -> 0 business cats
        "5": ["Cat B", "Cat F", "bogus-label"],  # differs from p1 -> ambiguous
        "6": ["Cat C"],
        "7": [],                            # empty: p6's set still wins
    }
    mp, wp = _tmp_json(mapping), _tmp_json(web_class)
    try:
        idx = rf.PrevWebsiteClassIndex.load(mp, wp, valid, ISSUE)
    finally:
        os.unlink(mp); os.unlink(wp)

    assert idx.stats.prev_prompts == 7
    assert idx.stats.dropped_prompts_no_key == 1
    assert idx.stats.invalid_category_labels["bogus-label"] == 1
    assert idx.stats.ambiguous_keys == 1       # foo.com
    assert idx.stats.zero_category_keys == 1   # baz.com

    # foo.com: p1={A,B} vs p5={B,F} -> conflicting -> ambiguous -> Terra
    r = idx.lookup(["https://foo.com/"])
    assert r.status == rf.PREV_AMBIGUOUS
    assert r.ambiguous and not r.has_previous_classification
    assert r.prior_category_count is None
    assert set(r.categories) == {"Cat A", "Cat B", "Cat F"}   # union kept for inspection

    # bar.com -> single consistent set of 5 -> ok
    r = idx.lookup(["https://www.bar.com/"])
    assert r.status == rf.PREV_OK and r.prior_category_count == 5

    # baz.com -> matched but only Website issue -> zero_categories -> not usable
    r = idx.lookup(["https://baz.com"])
    assert r.status == rf.PREV_ZERO_CATEGORIES
    assert not r.has_previous_classification and r.prior_category_count is None

    # qux.com -> p6 {C}, p7 {} -> empty is not a conflict -> ok, count 1
    r = idx.lookup(["https://qux.com"])
    assert r.status == rf.PREV_OK and r.prior_category_count == 1
    assert set(r.categories) == {"Cat C"}

    # unknown -> missing
    r = idx.lookup(["https://unknown-abc.com"])
    assert r.status == rf.PREV_MISSING
    assert not r.has_previous_classification and r.prior_category_count is None

    # dropped (no key) -> missing
    assert idx.lookup(["chrome-error://chromewebdata/"]).status == rf.PREV_MISSING


def test_lookup_multi_key_hit():
    ISSUE = "Website issue - Cannot determine categories"
    valid = {"X", "Y", "Z"}
    mapping = {
        "a": {"asns": ["1"], "landing_urls": ["https://one.com"]},
        "b": {"asns": ["2"], "landing_urls": ["https://two.com"]},
        "c": {"asns": ["3"], "landing_urls": ["https://three.com"]},
    }
    web_class = {"1": ["X", "Y"], "2": ["X", "Y"], "3": ["Z"]}
    mp, wp = _tmp_json(mapping), _tmp_json(web_class)
    try:
        idx = rf.PrevWebsiteClassIndex.load(mp, wp, valid, ISSUE)
    finally:
        os.unlink(mp); os.unlink(wp)

    # current prompt whose landing set matches one.com + two.com (same set) -> ok
    r = idx.lookup(["https://one.com/", "http://www.two.com"])
    assert r.status == rf.PREV_OK and set(r.categories) == {"X", "Y"}

    # matches one.com + three.com (different sets) -> ambiguous
    r = idx.lookup(["https://one.com/", "https://three.com/"])
    assert r.status == rf.PREV_AMBIGUOUS


def test_complete_prompt_token_count():
    # word-count stand-in tokenizer
    tc = lambda s: len(s.split())
    n = rf.complete_prompt_token_count("a b c", "d e", "f g h i", tc, framing_overhead_tokens=10)
    assert n == 3 + 2 + 4 + 10


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} routing-feature tests passed")
