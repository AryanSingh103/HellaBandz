"""Allocator bounds, scoring gates, and I-10 manifest integrity."""

import json

import pytest

from hellabandz.agents.tournament import CAP_PER_STRATEGY, DEPLOYABLE, SUPERVISOR_BAND, allocate
from hellabandz.audit.solana import manifest_hash_file, memo_for, verify
from hellabandz.contracts import StrategyFamily, StrategyProposal
from hellabandz.agents.tournament import expand_grid


def test_allocator_caps_and_cash_reserve():
    w, _ = allocate({"a": 5.0, "b": 0.1, "c": 0.0}, None)
    assert w["c"] == 0.0
    assert all(v <= CAP_PER_STRATEGY for v in w.values())
    assert sum(w.values()) <= DEPLOYABLE + 1e-9


def test_supervisor_cannot_move_weight_beyond_band_or_fund_failed_strategy():
    scores = {"a": 1.0, "b": 1.0, "c": 0.0}
    w_plain, d0 = allocate(scores, None)
    w_sup, d1 = allocate(scores, {"a": 1.0, "b": 0.0, "c": 1.0})
    assert w_sup["c"] == 0.0  # LLM cannot fund a strategy that failed validation gates
    base = d0["score_weights"]
    for k in ("a", "b"):
        assert abs(d1["blended"][k] - base[k]) <= SUPERVISOR_BAND / 2 + 1e-9


def test_all_failed_means_all_cash():
    w, _ = allocate({"a": 0.0, "b": 0.0}, {"a": 1.0})
    assert sum(w.values()) == 0


def test_llm_grid_outside_whitelist_bounds_is_dropped():
    p = StrategyProposal(proposal_id="p", agent_id="momentum", family=StrategyFamily.SMA_CROSSOVER,
                         universe=["SPY"], param_grid={"fast": [5, 10, 10_000], "slow": [3, 50]}, rationale="")
    combos = expand_grid(p)
    assert combos == [{"fast": 5, "slow": 50}, {"fast": 10, "slow": 50}]


def test_tampered_manifest_fails_verification(tmp_path):
    """I-10: altering any field of a committed manifest breaks verification."""
    m = {"run_id": "T-test", "allocations": {"by_strategy": {"x": {"weight": 0.4}}}, "nested": [1, 2.5, "a"]}
    path = tmp_path / "T-test.json"
    path.write_text(json.dumps(m, indent=1))
    h, _ = manifest_hash_file(path)
    path.with_suffix(".solana.json").write_text(json.dumps({"manifest_sha256": h, "memo": memo_for("T-test", h),
                                                            "signature": None}))
    assert verify(path, offline=True)["ok"]
    m["allocations"]["by_strategy"]["x"]["weight"] = 0.41
    path.write_text(json.dumps(m, indent=1))
    assert not verify(path, offline=True)["ok"]


def test_manifest_hash_is_formatting_independent(tmp_path):
    m = {"b": 1, "a": {"y": [1, 2], "x": 0.1}}
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    p1.write_text(json.dumps(m, indent=4))
    p2.write_text(json.dumps(m, separators=(",", ":"), sort_keys=True))
    assert manifest_hash_file(p1)[0] == manifest_hash_file(p2)[0]


@pytest.mark.parametrize("bad", [{"family": "arbitrary_python"}, {"universe": []}])
def test_proposal_schema_rejects_unlisted_family_or_empty_universe(bad):
    base = {"proposal_id": "p", "agent_id": "a", "family": "sma_crossover", "universe": ["SPY"],
            "param_grid": {"fast": [5], "slow": [10]}, "rationale": ""}
    with pytest.raises(ValueError):
        StrategyProposal.model_validate({**base, **bad})
