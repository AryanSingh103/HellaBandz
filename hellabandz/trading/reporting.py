"""Immutable-input provenance and exact virtual-sleeve reports for managed replays.

Financial amounts in this contract are integer micro quote units. The legacy
tournament is only an optional dataset anchor; it never supplies allocations.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from hellabandz.trading.registry import Registry
from hellabandz.trading.research import POLICY_VERSION, portfolio_sleeves, research_current
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


def report_path(directory: Path, portfolio_id: str) -> Path:
    return directory / f"{portfolio_id}.coordinated.json"


def save_report(directory: Path, portfolio_id: str, report: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".coordinated-", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(report, f, separators=(",", ":"), allow_nan=False)
        for attempt in range(6):
            try:
                os.replace(temp, report_path(directory, portfolio_id))
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_report(directory: Path, portfolio_id: str) -> dict | None:
    path = report_path(directory, portfolio_id)
    if not path.is_file():
        return None
    report = json.loads(path.read_text())
    if report.get("portfolio_id") != portfolio_id or report.get("schema") != "coordinated-simulation-1":
        raise ValueError("coordinated report identity mismatch")
    return report
