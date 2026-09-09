"""Standalone tests for write_routed_artifacts (run: python3 as2biz/test_routed_artifacts.py).
Needs the batch script's imports available."""

import json
import tempfile
from pathlib import Path

import prepare_openai_batch_process as m


def _line(cid, model):
    return {
        "line": json.dumps({"custom_id": cid, "body": {"model": model}}),
        "tokens": 100,
        "prompt_id": cid,
        "prompt_hash": cid + "hash",
        "model": model,
    }


def _mapping(pairs):
    # pairs: list of (prompt_id, model, [asns])
    return {pid: {"model": model, "asns": [str(a) for a in asns]} for pid, model, asns in pairs}


LUNA = m.routing.MODEL_LUNA
TERRA = m.routing.MODEL_TERRA


def test_happy_path_partitions_and_writes():
    lines = [_line("p1", LUNA), _line("p2", TERRA), _line("p3", LUNA)]
    mp = _mapping([("p1", LUNA, [1, 2]), ("p2", TERRA, [3]), ("p3", LUNA, [4])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        man = m.write_routed_artifacts(lines, mp, out, "deadbeefcafe0000", 180_000_000)
        assert man["total_prompts"] == 3
        assert man["total_asns"] == 4
        assert man["per_model"][LUNA]["prompt_count"] == 2
        assert man["per_model"][TERRA]["prompt_count"] == 1
        assert man["per_model"][LUNA]["asn_count"] == 3
        # files exist
        assert (out / LUNA / "batch_input.jsonl").exists()
        assert (out / TERRA / "batch_input.jsonl").exists()
        assert (out / LUNA / "batch_input_chunk_manifest.json").exists()
        assert (out / "routing_manifest.json").exists()
        assert (out / "routing_mapping.json").exists()
        # luna jsonl has exactly p1, p3
        luna_ids = {json.loads(l)["custom_id"]
                    for l in (out / LUNA / "batch_input.jsonl").read_text().splitlines()}
        assert luna_ids == {"p1", "p3"}
        cm = json.loads((out / LUNA / "batch_input_chunk_manifest.json").read_text())
        assert cm["build_id"].endswith(f"__{LUNA}__{m.routing.ROUTING_POLICY_VERSION}")
        assert cm["prompt_contract"] == m.PROMPT_CONTRACT


def _expect_raises(fn, needle):
    try:
        fn()
    except RuntimeError as e:
        assert needle in str(e), f"got {e!r}, expected substring {needle!r}"
    else:
        raise AssertionError(f"expected RuntimeError containing {needle!r}")


def test_duplicate_custom_id_across_streams_raises():
    lines = [_line("dup", LUNA), _line("dup", TERRA)]
    mp = _mapping([("dup", LUNA, [1])])
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, Path(d), "x" * 12, 180_000_000),
            "duplicate custom_id",
        )


def test_asn_in_two_streams_raises():
    lines = [_line("p1", LUNA), _line("p2", TERRA)]
    mp = _mapping([("p1", LUNA, [7]), ("p2", TERRA, [7])])  # ASN 7 in both
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, Path(d), "x" * 12, 180_000_000),
            "maps to both",
        )


def test_mapping_prompt_not_in_stream_raises():
    lines = [_line("p1", LUNA)]
    mp = _mapping([("p1", LUNA, [1]), ("ghost", TERRA, [2])])
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, Path(d), "x" * 12, 180_000_000),
            "not in any model stream",
        )


def test_missing_model_on_task_raises():
    bad = _line("p1", LUNA)
    bad["model"] = None
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts([bad], _mapping([("p1", LUNA, [1])]),
                                             Path(d), "x" * 12, 180_000_000),
            "has no model",
        )


def test_line_model_mismatch_raises():
    bad = _line("p1", LUNA)              # item["model"] = LUNA
    bad["line"] = json.dumps({"custom_id": "p1", "body": {"model": TERRA}})  # line says TERRA
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts([bad], _mapping([("p1", LUNA, [1])]),
                                             Path(d), "x" * 12, 180_000_000),
            "line model",
        )


def test_line_custom_id_mismatch_raises():
    bad = _line("p1", LUNA)
    bad["line"] = json.dumps({"custom_id": "OTHER", "body": {"model": LUNA}})
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts([bad], _mapping([("p1", LUNA, [1])]),
                                             Path(d), "x" * 12, 180_000_000),
            "line custom_id",
        )


def test_mapping_model_mismatch_raises():
    lines = [_line("p1", LUNA)]
    mp = _mapping([("p1", TERRA, [1])])   # mapping says the prompt is TERRA
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, Path(d), "x" * 12, 180_000_000),
            "!= mapping model",
        )


def test_task_without_mapping_entry_raises():
    lines = [_line("p1", LUNA)]
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, {}, Path(d), "x" * 12, 180_000_000),
            "no mapping entry",
        )


def test_token_cap_violation_raises():
    lines = [_line("p1", LUNA)]
    lines[0]["tokens"] = 999_999
    mp = _mapping([("p1", LUNA, [1])])
    with tempfile.TemporaryDirectory() as d:
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, Path(d), "x" * 12, 1000),
            "tokens over cap",
        )


def test_overwrite_policy_by_build_signature():
    lines = [_line("p1", LUNA), _line("p2", TERRA)]
    mp = _mapping([("p1", LUNA, [1]), ("p2", TERRA, [2])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        # same build_signature -> idempotent no-op, returns existing manifest
        man = m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        assert man["build_signature"] == "b" * 16
        # even with a submitted job present, same build is still a no-op
        (out / LUNA / "batch_jobs.json").write_text('{"batch_input.part1.jsonl": "batch_abc"}')
        m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        # different build_signature -> refuse
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, out, "c" * 16, 180_000_000),
            "different build_signature",
        )
        # ... force overrides
        man2 = m.write_routed_artifacts(lines, mp, out, "c" * 16, 180_000_000, force=True)
        assert man2["build_signature"] == "c" * 16


def test_routed_dir_name_carries_build_id():
    lines = [_line("p1", LUNA)]
    mp = _mapping([("p1", LUNA, [1])])
    with tempfile.TemporaryDirectory() as d:
        a = Path(d) / "routed" / "contract__routing__aaaaaaaaaaaa"
        b = Path(d) / "routed" / "contract__routing__bbbbbbbbbbbb"
        m.write_routed_artifacts(lines, mp, a, "a" * 16, 180_000_000)
        m.write_routed_artifacts(lines, mp, b, "b" * 16, 180_000_000)
        assert (a / "routing_manifest.json").exists()
        assert (b / "routing_manifest.json").exists()


def test_recover_model_from_task_line():
    good = json.dumps({"custom_id": "x", "body": {"model": "gpt-5.6-luna"}})
    assert m.recover_model_from_task_line(good) == "gpt-5.6-luna"
    assert m.recover_model_from_task_line("{not json") is None
    assert m.recover_model_from_task_line(json.dumps({"body": {}})) is None


class _Args:
    def __init__(self, **kw):
        self.prev_mapping = kw.get("prev_mapping")
        self.prev_web_class = kw.get("prev_web_class")
        self.prev_version_tag = kw.get("prev_version_tag")
        self.version_tag = kw.get("version_tag", "2026-09-01")


def test_resolve_prev_routing_files():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for tag in ("2026-01-01", "2026-03-01"):
            (tmp / tag).mkdir()
            (tmp / tag / "batch_input_mapping.json").write_text("{}")
            (tmp / tag / "batch_input_as2biz_main.json").write_text("{}")
        (tmp / "2026-06-01").mkdir()
        (tmp / "2026-06-01" / "batch_input_mapping.json").write_text("{}")  # incomplete

        # auto-discover: newest complete pair strictly older than version_tag
        mp, wc, src, explicit = m.resolve_prev_routing_files(_Args(), tmp)
        assert Path(mp).parent.name == "2026-03-01" and "auto-discovered" in src
        assert explicit is False

        # explicit --prev-version-tag wins and is flagged explicit
        mp, wc, src, explicit = m.resolve_prev_routing_files(
            _Args(prev_version_tag="2026-01-01"), tmp)
        assert Path(mp).parent.name == "2026-01-01" and explicit is True

        # --prev-version-tag that does not exist -> (None, None, ..., explicit=True)
        mp, wc, src, explicit = m.resolve_prev_routing_files(
            _Args(prev_version_tag="2099-01-01"), tmp)
        assert mp is None and explicit is True and "not found" in src

        # incomplete explicit pair -> (None, None, ..., explicit=True)
        mp, wc, src, explicit = m.resolve_prev_routing_files(
            _Args(prev_mapping="/x/m.json"), tmp)
        assert mp is None and explicit is True and "need both" in src

        # both explicit paths given but one does not exist -> hard-stop signal
        mp, wc, src, explicit = m.resolve_prev_routing_files(
            _Args(prev_mapping=str(tmp / "2026-01-01" / "batch_input_mapping.json"),
                  prev_web_class="/nope/does_not_exist.json"), tmp)
        assert mp is None and explicit is True and "does not exist" in src

        # both explicit paths exist -> returned as-is, explicit
        mp, wc, src, explicit = m.resolve_prev_routing_files(
            _Args(prev_mapping=str(tmp / "2026-01-01" / "batch_input_mapping.json"),
                  prev_web_class=str(tmp / "2026-01-01" / "batch_input_as2biz_main.json")),
            tmp)
        assert mp and wc and explicit is True

        # nothing older -> (None, None, ..., explicit=False)
        mp, wc, src, explicit = m.resolve_prev_routing_files(
            _Args(version_tag="2020-01-01"), tmp)
        assert mp is None and explicit is False and "no prior snapshot" in src


def test_force_rebuild_supersedes_stale_job_records():
    lines = [_line("p1", LUNA), _line("p2", TERRA)]
    mp = _mapping([("p1", LUNA, [1]), ("p2", TERRA, [2])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        (out / LUNA / "batch_jobs.json").write_text('{"batch_input.part1.jsonl": "old"}')
        # force rebuild with a NEW build_signature -> old job record must be moved aside
        m.write_routed_artifacts(lines, mp, out, "c" * 16, 180_000_000, force=True)
        assert not (out / LUNA / "batch_jobs.json").exists()
        assert list((out / LUNA).glob("batch_jobs.superseded-*.json"))


def test_manifest_records_artifact_hashes():
    lines = [_line("p1", LUNA), _line("p2", TERRA)]
    mp = _mapping([("p1", LUNA, [1]), ("p2", TERRA, [2])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        man = m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        assert man["artifact_hashes_version"] == m.ARTIFACT_HASHES_VERSION
        assert len(man["mapping_sha256"]) == 64
        for mm in (LUNA, TERRA):
            info = man["per_model"][mm]
            assert info["request_count"] == 1
            assert len(info["custom_ids_sha256"]) == 64
            assert "batch_input.jsonl" in info["files"]
            assert any(f.startswith("batch_input.part") for f in info["files"])
        # a clean build verifies
        m.verify_routed_artifacts(out, man)


def test_verify_rejects_hashless_legacy_manifest():
    # a manifest with no artifact_hashes_version cannot be integrity checked;
    # that is a hard error (fail-closed), not a warning
    _expect_raises(
        lambda: m.verify_routed_artifacts(Path("/tmp"), {"per_model": {}}),
        "predates artifact hashing",
    )


def test_same_signature_reuse_detects_tampered_part():
    lines = [_line("p1", LUNA), _line("p2", TERRA)]
    mp = _mapping([("p1", LUNA, [1]), ("p2", TERRA, [2])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        part = out / LUNA / "batch_input.part1.jsonl"
        part.write_text(part.read_text() + '{"custom_id":"injected","body":{"model":"gpt-5.6-luna"}}\n')
        # same build_signature -> reuse path must re-hash and reject the edit
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000),
            "integrity check failed",
        )


def test_same_signature_reuse_detects_extra_part_file():
    lines = [_line("p1", LUNA)]
    mp = _mapping([("p1", LUNA, [1])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        (out / LUNA / "batch_input.part99.jsonl").write_text('{"custom_id":"x"}\n')
        _expect_raises(
            lambda: m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000),
            "not in the manifest",
        )


def test_completion_budget_is_per_request_sum():
    lines = [_line("p1", LUNA), _line("p2", LUNA)]
    mp = {
        "p1": {"model": LUNA, "asns": ["1"], "max_completion_tokens": 10},
        "p2": {"model": LUNA, "asns": ["2"], "max_completion_tokens": 20},
    }
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        man = m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        # 10 + 20, NOT 2 * max(10, 20) == 40
        assert man["per_model"][LUNA]["est_completion_budget_tokens"] == 30


def test_resolve_model_prices_precedence_and_gaps():
    import types
    models = [LUNA, TERRA]

    a = types.SimpleNamespace(model_price=[f"{LUNA}:0.05:0.40"],
                              batch_input_price_per_1m=None,
                              batch_output_price_per_1m=None)
    resolved, missing = m.resolve_model_prices(a, models)
    assert resolved[LUNA] == (0.05, 0.40)
    assert resolved[TERRA] == (2.00, 12.00)
    assert missing == []

    # Known routed models use their built-in defaults even when a shared price
    # is incomplete.
    a = types.SimpleNamespace(model_price=None,
                              batch_input_price_per_1m=0.1,
                              batch_output_price_per_1m=None)
    resolved, missing = m.resolve_model_prices(a, models)
    assert resolved == {LUNA: (0.20, 1.20), TERRA: (2.00, 12.00)}
    assert missing == []

    # Exact per-model overrides beat built-in defaults. The shared fallback is
    # reserved for models that do not have a built-in price.
    a = types.SimpleNamespace(model_price=[f"{TERRA}:0.55:4.40"],
                              batch_input_price_per_1m=0.05,
                              batch_output_price_per_1m=0.40)
    resolved, missing = m.resolve_model_prices(a, models)
    assert resolved[TERRA] == (0.55, 4.40)
    assert resolved[LUNA] == (0.20, 1.20)
    assert missing == []

    a = types.SimpleNamespace(model_price=None,
                              batch_input_price_per_1m=0.05,
                              batch_output_price_per_1m=0.40)
    resolved, missing = m.resolve_model_prices(a, ["custom-model"])
    assert resolved == {"custom-model": (0.05, 0.40)} and missing == []


def test_resolve_model_prices_rejects_nan_and_negative():
    import types
    for bad in (f"{LUNA}:nan:1", f"{LUNA}:-1:2", f"{LUNA}:inf:2", f"{LUNA}:1:-0.5"):
        a = types.SimpleNamespace(model_price=[bad],
                                  batch_input_price_per_1m=None,
                                  batch_output_price_per_1m=None)
        try:
            m.resolve_model_prices(a, [LUNA])
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError(f"expected SystemExit for --model-price {bad!r}")
    # negative shared fallback is rejected too
    a = types.SimpleNamespace(model_price=None,
                              batch_input_price_per_1m=-3.0,
                              batch_output_price_per_1m=1.0)
    try:
        m.resolve_model_prices(a, [LUNA])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("expected SystemExit for negative --batch-input-price-per-1m")


def test_assert_submit_complete_flags_missing_job_id():
    lines = [_line("p1", LUNA), _line("p2", TERRA)]
    mp = _mapping([("p1", LUNA, [1]), ("p2", TERRA, [2])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        man = m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        # only Luna's part recorded -> Terra part has no job id -> exit 2
        (out / LUNA / "batch_jobs.json").write_text('{"batch_input.part1.jsonl": "batch_luna"}')
        try:
            m._assert_submit_complete(out, man)
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("expected SystemExit(2) for the un-submitted Terra part")
        # now record Terra too -> passes
        (out / TERRA / "batch_jobs.json").write_text('{"batch_input.part1.jsonl": "batch_terra"}')
        m._assert_submit_complete(out, man)  # no raise


class _NoUploadClient:
    """A client whose every batch/file call fails the test if reached."""
    class _Boom:
        def __getattr__(self, _n):
            def _f(*a, **k):
                raise AssertionError("submit_routed_artifacts uploaded despite a bad artifact")
            return _f
    batches = _Boom()
    files = _Boom()


def test_submit_refuses_when_part_hash_mismatches_manifest():
    lines = [_line("p1", LUNA), _line("p2", TERRA)]
    mp = _mapping([("p1", LUNA, [1]), ("p2", TERRA, [2])])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "routed"
        m.write_routed_artifacts(lines, mp, out, "b" * 16, 180_000_000)
        part = out / TERRA / "batch_input.part1.jsonl"
        part.write_text(part.read_text().replace("p2", "p2X"))
        res = m.submit_routed_artifacts(_NoUploadClient(), out)
        assert res == {}  # refused before any upload


def test_routed_dir_lock_is_exclusive():
    if m._fcntl is None:
        return  # lock is a documented no-op without fcntl
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        with m.routed_dir_lock(rd):
            try:
                with m.routed_dir_lock(rd):
                    raise AssertionError("second lock acquisition should have failed")
            except RuntimeError as e:
                assert "another submit is already running" in str(e)
        # released -> can be taken again
        with m.routed_dir_lock(rd):
            pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} routed-artifacts tests passed")
