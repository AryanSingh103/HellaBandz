import json
import copy
from pathlib import Path

import pytest

from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.trading import live
from hellabandz.trading.execution import LocalLedger
from hellabandz.trading.registry import Registry
from hellabandz.trading.reporting import load_report, research_context, save_report, simulation_context
from hellabandz.trading.research import POLICY_VERSION, research_current
from hellabandz.trading.strategies import CODE_VERSION, make_version

ROOT = Path(__file__).resolve().parents[2]


def prepared(tmp_path):
    _, meta = load_snapshot(ROOT / "data/fixtures/synthetic_1h")
    reg = Registry(tmp_path / "registry.json")
    old = make_version("trend", "trend", {"fast": 8, "slow": 40}, meta["symbols"])
    chosen = make_version("trend", "trend", {"fast": 12, "slow": 48}, meta["symbols"])
    for v in (old, chosen):
        key = reg.propose("trend", v, "test fixture")
        reg.record_evaluation(key, {}, {}, False, ["RETURN_NOT_ABOVE_BASELINE"])
    reg.data["research_state"] = {"status": "complete", "policy_version": POLICY_VERSION,
        "code_version": CODE_VERSION, "dataset_sha256": meta["content_sha256"],
        "frozen_challengers": {"trend": chosen.describe()}, "roles": []}
    reg._save()
    return meta, reg, chosen


def test_only_current_frozen_challenger_is_reported(tmp_path):
    meta, reg, chosen = prepared(tmp_path)
    context = research_context(reg, meta)
    assert context["current"]
    assert [(a["agent_id"], a["version"], a["funded"]) for a in context["agents"] if a["agent_id"] != "core"] == [
        ("trend", chosen.version, False)]
    assert next(a for a in context["agents"] if a["agent_id"] == "core")["capital"] == 9_000_000_000
    reg.data["research_state"]["code_version"] = "old"
    assert not research_context(reg, meta)["current"]
    assert research_context(reg, meta)["agents"] == []


def test_frozen_identity_cannot_be_silently_recreated_under_new_code(tmp_path):
    meta, reg, chosen = prepared(tmp_path)
    reg.data["versions"][f"trend:{chosen.version}"]["params"]["fast"] = 16
    with pytest.raises(ValueError, match="identity mismatch"):
        research_context(reg, meta)


def test_report_identity_and_atomic_round_trip(tmp_path):
    report = {"schema": "coordinated-simulation-1", "portfolio_id": "fresh", "points": []}
    save_report(tmp_path, "fresh", report)
    assert load_report(tmp_path, "fresh") == report
    assert load_report(tmp_path, "missing") is None
    report["portfolio_id"] = "other"
    save_report(tmp_path, "fresh", report)
    with pytest.raises(ValueError, match="identity"):
        load_report(tmp_path, "fresh")


@pytest.mark.parametrize("mode", ["approved", "experimental"])
def test_managed_runner_reports_every_rule_and_matches_truncated_baseline(tmp_path, monkeypatch, mode):
    meta, reg, chosen = prepared(tmp_path)
    messages = []
    class Response:
        def json(self):
            return {"match": True, "chain_ok": True}
    class Client:
        def get(self, *a, **kw):
            return Response()
        def post(self, path, json):
            messages.append(json)
            return Response()
    class TestLedger(LocalLedger):
        def __init__(self, api, pid, headers, run):
            super().__init__()
            self.pid, self.c, self.latency_ns, self.calls = pid, Client(), [], 0
    monkeypatch.setattr(live, "Registry", lambda path: reg)
    monkeypatch.setattr(live, "ApiLedger", TestLedger)
    monkeypatch.setattr(live, "MANIFESTS", tmp_path)
    progress = []
    before = copy.deepcopy(reg.data)
    result = live.run_coordinated({"config": {"steps": 10, "speed": 0, "allocation_mode": mode}, "portfolio_id": "fresh",
        "run_id": "run"}, ROOT / "data/fixtures/synthetic_1h", "http://test", {},
        lambda *args: progress.append(args), lambda: len([x for x in progress if x[0] == "coordinating"]) >= 3)
    report = load_report(tmp_path, "fresh")
    assert result["cycles"] == 3 and report["status"] == "stopped"
    assert len(report["rule_readings"]) == 3 * 2  # current challenger + core, every cycle
    assert len(report["benchmark"]["baseline"]) == len(report["points"]) == 3
    assert report["benchmark"]["end_ts"] == report["steps"][-1]["ts"] + 1
    if mode == "approved":
        assert report["benchmark"]["baseline_metrics"]["net_return"] == result["net_return"]
    assert result["reconciliation_breaks"] == result["ledger_rejections"] == 0
    if mode == "approved":
        assert all(p["agents"]["trend"] == {"pnl": 0, "exposure": 0} for p in report["points"])
    else:
        assert all(a["funded"] and a["capital"] == 4_500_000_000 for a in report["context"]["agents"])
        assert report["context"]["allocation_mode"] == "experimental"
        assert reg.data == before
        assert report["context"]["agents"][0]["status"] == "rejected"
        assert report["context"]["agents"][0]["research_approved"] is False
    assert sum(s["equity"] for s in result["attribution"].values()) + result["unallocated_cash"] == round(result["final_equity"] * 1e6)
    assert [m for m in messages if m["role"] == "coordinator_start"][0]["content"]["context"]["agents"][0]["version"] == chosen.version
    stages = [p[0] for p in progress]
    assert stages[0] == "loading_market_data"
    assert stages.index("opening_paper_account") < stages.index("warming_strategy_history") < stages.index("coordinating")


def test_experimental_reuses_last_completed_research_without_changing_refresh(tmp_path):
    meta, reg, _ = prepared(tmp_path)
    completed = json.loads(json.dumps(reg.data["research_state"]))
    reg.data["research_runs"] = [completed]
    reg.data["research_state"] = {"status": "running"}
    before = copy.deepcopy(reg.data)
    context = simulation_context(reg, meta, "experimental")
    assert context["current"] and all(a["funded"] for a in context["agents"])
    assert sum(a["capital"] for a in context["agents"]) == 9_000_000_000
    assert reg.data == before
    assert not simulation_context(reg, meta, "approved")["current"]
    meta = {**meta, "content_sha256": "another dataset"}
    assert not simulation_context(reg, meta, "experimental")["current"]


def test_unknown_allocation_mode_is_rejected(tmp_path):
    meta, reg, _ = prepared(tmp_path)
    with pytest.raises(ValueError, match="allocation mode"):
        simulation_context(reg, meta, "unsafe")


def test_incomplete_research_never_starts_trading(tmp_path, monkeypatch):
    meta, reg, _ = prepared(tmp_path)
    reg.data["research_state"]["status"] = "stopped"
    monkeypatch.setattr(live, "Registry", lambda path: reg)
    monkeypatch.setattr(live, "research", lambda *a, **kw: {"status": "stopped"})
    with pytest.raises(ValueError, match="completed compatible research"):
        live.run_coordinated({"config": {}, "portfolio_id": "fresh", "run_id": "run"},
            ROOT / "data/fixtures/synthetic_1h", "http://test", {}, lambda *a: None, lambda: False)
