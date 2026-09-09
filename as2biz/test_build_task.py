"""Standalone tests for build_classification_task + response_format_schema
(run: python3 as2biz/test_build_task.py). Needs the batch script's imports
(tiktoken etc.) available."""

import json

import prepare_openai_batch_process as m


DEV = "developer instructions"
PREFIX = "TEMPLATE\n\nTAXONOMY\n\nDESCR\n\nSite Text:\n\n"
BODY = "### Landing\n\nhello world"


def _task(model, prompt_contract=m.PROMPT_CONTRACT):
    return m.build_classification_task(
        custom_id="bid:prompt:abcd",
        model=model,
        developer_prompt=DEV,
        stable_user_prefix=PREFIX,
        variable_body=BODY,
        temperature=0.0,
        max_completion_tokens=1024,
        prompt_contract=prompt_contract,
    )


def test_common_shape():
    t = _task("gpt-5.6-terra")
    assert t["custom_id"] == "bid:prompt:abcd"
    assert t["method"] == "POST"
    assert t["url"] == "/v1/chat/completions"
    b = t["body"]
    assert b["model"] == "gpt-5.6-terra"
    assert b["temperature"] == 0.0
    assert b["max_completion_tokens"] == 1024
    assert b["reasoning_effort"] == "none"
    assert b["messages"][0] == {"role": "developer", "content": DEV}
    assert b["response_format"] == m.response_format_schema()
    # strict schema, 97 enum ids
    js = b["response_format"]["json_schema"]
    assert js["strict"] is True
    assert len(js["schema"]["properties"]["category_ids"]["items"]["enum"]) == 97
    assert js["schema"]["additionalProperties"] is False


def test_gpt56_explicit_cache_layout():
    for model in ("gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"):
        b = _task(model)["body"]
        uc = b["messages"][1]["content"]
        assert isinstance(uc, list) and len(uc) == 2
        assert uc[0]["text"] == PREFIX
        assert uc[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert uc[1]["text"] == BODY
        assert "prompt_id" not in uc[0]["text"] and "prompt:abcd" not in uc[0]["text"]
        assert b["prompt_cache_key"].startswith("as2biz-")
        assert model.replace(".", "-") in b["prompt_cache_key"] or "gpt-5-6" in b["prompt_cache_key"]
        assert b["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}


def test_non_gpt56_flat_no_cache():
    b = _task("gpt-5.2")["body"]
    assert b["messages"][1]["content"] == PREFIX + BODY
    assert "prompt_cache_key" not in b
    assert "prompt_cache_options" not in b


def test_stable_prefix_is_byte_identical_across_models():
    a = _task("gpt-5.6-terra")["body"]["messages"][1]["content"][0]["text"]
    c = _task("gpt-5.6-luna")["body"]["messages"][1]["content"][0]["text"]
    assert a == c == PREFIX


def test_json_serializable():
    for model in ("gpt-5.6-terra", "gpt-5.2"):
        json.dumps(_task(model))


def test_contract_auto_boundary():
    assert m.resolve_prompt_contract("2026-08-31").name == m.LEGACY_PROMPT_CONTRACT
    assert m.resolve_prompt_contract("20260831-backfill").name == m.LEGACY_PROMPT_CONTRACT
    assert m.resolve_prompt_contract("2026-09-01").name == m.PROMPT_CONTRACT
    assert m.resolve_prompt_contract("2026-09").name == m.PROMPT_CONTRACT
    assert m.resolve_prompt_contract("snapshot_20260901").name == m.PROMPT_CONTRACT


def test_contract_explicit_override_and_invalid_date():
    assert m.resolve_prompt_contract("not-a-date", m.PROMPT_CONTRACT).name == m.PROMPT_CONTRACT
    try:
        m.resolve_prompt_contract("not-a-date", "auto")
    except ValueError as e:
        assert "cannot infer" in str(e)
    else:
        raise AssertionError("expected an ambiguous auto version tag to fail")


def test_legacy_task_uses_full_name_contract_without_schema():
    body = _task("gpt-5.2", m.LEGACY_PROMPT_CONTRACT)["body"]
    assert "response_format" not in body
    assert "prompt_cache_key" not in body
    assert body["messages"][1]["content"] == PREFIX + BODY


def test_contract_fingerprints_are_distinct():
    current = m.contract_fingerprint(m.PROMPT_CONTRACT)
    legacy = m.contract_fingerprint(m.LEGACY_PROMPT_CONTRACT)
    assert current["contract"] != legacy["contract"]
    assert current["response_schema_sha256"]
    assert legacy["response_schema_sha256"] is None
    assert current["developer_prompt_sha256"] != legacy["developer_prompt_sha256"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} build-task tests passed")
