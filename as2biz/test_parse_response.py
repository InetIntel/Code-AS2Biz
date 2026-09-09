"""Standalone tests for extract_response_parts + classify_response
(run: python3 as2biz/test_parse_response.py). Needs the batch script's imports."""

import json

import prepare_openai_batch_process as m

C1 = "C001"
C1_NAME = m.category_id_to_name_202609["C001"]
C2 = "C002"
C2_NAME = m.category_id_to_name_202609["C002"]


def _chat_line(*, content=None, refusal=None, finish_reason="stop", status_code=200,
               usage=None, error=None):
    msg = {}
    if content is not None:
        msg["content"] = content
    if refusal is not None:
        msg["refusal"] = refusal
    return {
        "custom_id": "b:prompt:x",
        "response": {"status_code": status_code,
                     "body": {"choices": [{"message": msg, "finish_reason": finish_reason}],
                              "usage": usage or {}}},
        "error": error,
    }


def test_ok_category_ids():
    obj = _chat_line(content=json.dumps({"category_ids": [C1, C2, C1]}),
                     usage={"prompt_tokens": 100,
                            "prompt_tokens_details": {"cached_tokens": 80}})
    parts = m.extract_response_parts(obj)
    assert parts["refusal"] is None
    assert parts["usage"]["prompt_tokens_details"]["cached_tokens"] == 80
    cats, status = m.classify_response(parts)
    assert status == m.PARSE_OK
    assert cats == [C1_NAME, C2_NAME]  # de-duplicated, order preserved


def test_website_issue_id_alone_is_ok():
    # C097 alone is a valid response: no business categories, not a violation
    obj = _chat_line(content=json.dumps({"category_ids": [m.WEBSITE_ISSUE_CATEGORY_ID]}))
    cats, status = m.classify_response(m.extract_response_parts(obj))
    assert status == m.PARSE_OK and cats == []


def test_empty_category_ids_is_contract_violation():
    obj = _chat_line(content=json.dumps({"category_ids": []}))
    cats, status = m.classify_response(m.extract_response_parts(obj))
    assert status == m.PARSE_CONTRACT_VIOLATION and cats == []


def test_unknown_or_nonstring_id_is_contract_violation():
    obj = _chat_line(content=json.dumps({"category_ids": ["C999", C1, "nope"]}))
    cats, status = m.classify_response(m.extract_response_parts(obj))
    # known IDs still extracted, but the presence of unknown IDs flags it
    assert status == m.PARSE_CONTRACT_VIOLATION and cats == [C1_NAME]
    obj2 = _chat_line(content=json.dumps({"category_ids": [C1, 5]}))
    cats2, status2 = m.classify_response(m.extract_response_parts(obj2))
    assert status2 == m.PARSE_CONTRACT_VIOLATION and cats2 == [C1_NAME]


def test_website_issue_must_be_used_alone():
    W = m.WEBSITE_ISSUE_CATEGORY_ID
    # C097 alongside a business ID -> violation (Website issue not alone)
    cats, status = m.classify_response(
        m.extract_response_parts(_chat_line(content=json.dumps({"category_ids": [W, C1]}))))
    assert status == m.PARSE_CONTRACT_VIOLATION and cats == [C1_NAME]
    # order does not matter
    cats, status = m.classify_response(
        m.extract_response_parts(_chat_line(content=json.dumps({"category_ids": [C1, W]}))))
    assert status == m.PARSE_CONTRACT_VIOLATION
    # C097 duplicated but still alone -> OK
    cats, status = m.classify_response(
        m.extract_response_parts(_chat_line(content=json.dumps({"category_ids": [W, W]}))))
    assert status == m.PARSE_OK and cats == []


def test_refusal_is_distinct_from_empty():
    obj = _chat_line(refusal="I'm sorry, I can't help with that.")
    parts = m.extract_response_parts(obj)
    assert parts["refusal"]
    cats, status = m.classify_response(parts)
    assert status == m.PARSE_REFUSAL and cats == []


def test_empty_response():
    cats, status = m.classify_response(m.extract_response_parts(_chat_line(content="")))
    assert status == m.PARSE_EMPTY and cats == []
    cats, status = m.classify_response(m.extract_response_parts(_chat_line()))
    assert status == m.PARSE_EMPTY


def test_invalid_json_no_name_match():
    cats, status = m.classify_response(m.extract_response_parts(_chat_line(content="totally not json")))
    assert status == m.PARSE_INVALID_JSON and cats == []


def test_legacy_name_match_fallback():
    # a pre-202609-style plain-text response naming a category
    cats, status = m.classify_response(
        m.extract_response_parts(_chat_line(content=f"The site is: {C1_NAME}")))
    assert status == m.PARSE_LEGACY_NAME_MATCH and C1_NAME in cats


def test_recorded_contract_does_not_guess_another_format():
    legacy_text = m.extract_response_parts(
        _chat_line(content=f"The site is: {C1_NAME}"))
    cats, status = m.classify_response(legacy_text, m.PROMPT_CONTRACT)
    assert status == m.PARSE_INVALID_JSON and cats == []

    current_json = m.extract_response_parts(
        _chat_line(content=json.dumps({"category_ids": [C1]})))
    cats, status = m.classify_response(current_json, m.LEGACY_PROMPT_CONTRACT)
    assert status == m.PARSE_INVALID_JSON and cats == []


def test_unknown_recorded_contract_is_rejected():
    try:
        m.classify_response({"text": C1_NAME}, "unknown-contract")
    except ValueError as exc:
        assert "unknown prompt contract" in str(exc)
    else:
        raise AssertionError("unknown prompt contract should fail closed")


def test_backcompat_wrapper():
    assert m.extract_valid_categories(json.dumps({"category_ids": [C1]})) == [C1_NAME]
    assert m.extract_valid_categories("") == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} parse-response tests passed")
