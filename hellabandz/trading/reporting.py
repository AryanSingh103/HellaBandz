"""Immutable-input provenance and exact virtual-sleeve reports for managed replays.

Financial amounts in this contract are integer micro quote units. The legacy
tournament is only an optional dataset anchor; it never supplies allocations.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import tempfile
import time
from pathlib import Path

from hellabandz.trading.registry import Registry
from hellabandz.trading.research import DEPLOYABLE, POLICY_VERSION, portfolio_sleeves, research_current
from hellabandz.trading.strategies import CODE_VERSION, FAMILY_CARDS, make_version

ROOT = Path(__file__).resolve().parents[2]


def source_hash() -> str:
    h = hashlib.sha256()
    paths = sorted((ROOT / "hellabandz/trading").glob("*.py")) + [
        ROOT / "hellabandz/api/workflows.py", ROOT / "hellabandz/execution/manager.py"]
    for path in paths:
        h.update(path.relative_to(ROOT).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def registry_path(dataset_id: str) -> Path:
    return ROOT / "data/registry" / f"{dataset_id}.json"


def research_context(reg: Registry, meta: dict) -> dict:
    current = research_current(reg, meta)
    state = reg.data.get("research_state", {})
    agents = []
    sleeves = portfolio_sleeves(reg, sorted(meta["symbols"])) if current else []
    funded = {sid: (v, cap) for sid, v, cap in sleeves}
    # Only the challengers frozen by THIS completed research run are displayed.
    # Historical rejected versions are evidence, never a replacement strategy.
    for role, description in sorted(state.get("frozen_challengers", {}).items()) if current else []:
        rec = reg.data.get("versions", {}).get(f"{role}:{description['version']}")
        if not rec or rec.get("code_version") != CODE_VERSION:
            raise ValueError("frozen challenger is absent or incompatible")
        version = make_version(role, FAMILY_CARDS[rec["family"]], rec["params"], rec["universe"])
        if version.version != description["version"] or not set(version.universe) <= set(meta["symbols"]):
            raise ValueError("frozen challenger identity mismatch")
        actual = funded.get(role)
        # A funded champion may differ from a rejected challenger: show both
        # identities explicitly rather than relabeling the champion as rejected.
        if actual and actual[0].version != version.version:
            agents.append({**actual[0].describe(), "agent_id": role, "funded": True,
                           "capital": actual[1], "status": "champion", "reasons": []})
            role = role + "_challenger"
        agents.append({**version.describe(), "agent_id": role, "funded": bool(actual and role in funded),
                       "capital": actual[1] if actual and role in funded else 0,
                       "status": rec["status"], "reasons": rec.get("decision_reasons", []),
                       "proposal_source": rec.get("source"), "evaluation": rec.get("evaluation"),
                       "strategy_id": role})
    for sid, (version, cap) in funded.items():
        if not any(a["agent_id"] == sid for a in agents):
            agents.append({**version.describe(), "agent_id": sid, "capital": cap,
                           "funded": True, "status": "passive" if sid == "core" else "champion", "reasons": []})
    return {"current": current, "policy_version": POLICY_VERSION, "code_version": CODE_VERSION,
            "source_sha256": source_hash(), "dataset": meta, "agents": agents,
            "registry_sha256": hashlib.sha256(json.dumps(reg.data, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "research": state if current else {"status": state.get("status", "missing"),
                "reason": "Current-policy research must complete before execution; legacy allocations are ignored."},
            "depth": "synthetic OHLCV liquidity ladder; not exchange order-book depth",
            "commentary": "Optional language-model remarks cannot change fixed rules or place orders.",
            "evaluation_status": "exploratory historical replay; this period has previously been inspected"}


def simulation_context(reg: Registry, meta: dict, allocation_mode: str = "approved") -> dict:
    """Paper experiments use frozen challengers without granting research approval.

    A concurrent research refresh must not hide an already completed frozen run.
    Read an isolated archived snapshot; never change registry promotions or budgets.
    """
    if allocation_mode not in ("approved", "experimental"):
        raise ValueError("unknown coordinated allocation mode")
    snapshot = copy.deepcopy(reg)
    if allocation_mode == "experimental" and not research_current(snapshot, meta):
        for state in reversed(snapshot.data.get("research_runs", [])):
            snapshot.data["research_state"] = state
            if research_current(snapshot, meta):
                break
    context = research_context(snapshot, meta)
    context["allocation_mode"] = allocation_mode
    if allocation_mode == "experimental" and context["current"]:
        frozen = snapshot.data["research_state"].get("frozen_challengers", {})
        selected = []
        for role in sorted(frozen):
            agent = next(a for a in context["agents"] if a["version"] == frozen[role]["version"]
                         and a["agent_id"] in (role, role + "_challenger"))
            selected.append({**agent, "agent_id": role, "strategy_id": role})
        selected += [a for a in context["agents"] if a["agent_id"] == "core"]
        cap, remainder = divmod(DEPLOYABLE, len(selected))
        context["agents"] = [{**a, "research_approved": a["funded"], "funded": True,
            "capital": cap + (remainder if a["agent_id"] == "core" else 0), "experimental": True}
            for a in selected]
        context["allocation_note"] = "Experimental paper budgets for every frozen challenger and passive core; research promotion gates are bypassed for this replay only. Not approved performance."
    else:
        context["allocation_note"] = "Only research-approved strategies receive paper capital; rejected challengers watch without trading."
    return context


def report_path(directory: Path, portfolio_id: str) -> Path:
    return directory / f"{portfolio_id}.coordinated.json"


def live_report(report: dict) -> dict:
    """Completed-cycle view for polling; omit the growing decision/reading archive."""
    return {**{k: v for k, v in report.items() if k not in ("decisions", "rule_readings", "steps", "telemetry")},
            "steps": report.get("steps", [])[-1:]}


def save_report(directory: Path, portfolio_id: str, report: dict, *, live: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".coordinated-", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(report, f, separators=(",", ":"), allow_nan=False)
        for attempt in range(6):
            try:
                os.replace(temp, directory / f"{portfolio_id}.coordinated.live.json" if live else report_path(directory, portfolio_id))
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_report(directory: Path, portfolio_id: str, *, live: bool = False) -> dict | None:
    path = directory / f"{portfolio_id}.coordinated.live.json" if live else report_path(directory, portfolio_id)
    if live and not path.is_file():
        report = load_report(directory, portfolio_id)
        return live_report(report) if report else None
    if not path.is_file():
        return None
    report = json.loads(path.read_text())
    if report.get("portfolio_id") != portfolio_id or report.get("schema") != "coordinated-simulation-1":
        raise ValueError("coordinated report identity mismatch")
    return report
