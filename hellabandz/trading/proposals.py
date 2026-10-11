"""Bounded research agents. Proposals are data, never executable generated code.

The deterministic agents use development-only summaries. Optional LLM proposals use
the same summary and are additive to the house hypotheses. A critic's development
feedback changes the next optimization round, never the validation/test data.
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass

from hellabandz.contracts import StrategyFamily, StrategyProposal
from hellabandz.strategies import ALLOCATION_FAMILIES, FAMILIES, validate_params
from hellabandz.trading.instruments import SECTOR_ETFS
from hellabandz.trading.strategies import CARDS, FAMILY_CARDS, StrategyVersion, make_version

ROLE_CARDS = {"trend": ("trend", "breakout", "momentum", "rotation"),
              "mean_reversion": ("mean_reversion", "rsi", "regime"),
              "defensive": ("defensive", "regime", "risk_rotation")}


def defensive_challenger_proposals(universe: list[str]) -> list[Proposal]:
    """A paired regime grid; no alphabetical mutation starvation.

    More selective stretch exits, faster reentry and longer smoothing test cash
    drag without changing execution or sizing. The grid is fixed before any
    validation feedback and every point is recorded, including failures.
    """
    return [Proposal(make_version("defensive", card, {
                "regime_lookback": 200, "lookback": lookback, "min_reversion": speed,
                "sell_z": sell, "rebuy_z": rebuy}, universe),
            "defensive-challenger-grid-2", CARDS[card].hypothesis,
            feedback="fixed complete grid: longer horizon, more selective exits and earlier reentry")
            for card, lookback, speed, sell, rebuy in itertools.product(
                ("regime", "regime_units"), (100, 200), (.025, .075), (1.5, 2.5), (0.0, .5))]


def allocation_universe(universe: list[str]) -> list[str]:
    sectors = sorted(set(universe) & set(SECTOR_ETFS))
    return sectors if len(sectors) >= 3 else sorted(universe)


@dataclass(frozen=True)
class Proposal:
    version: StrategyVersion
    source: str
    hypothesis: str
    parent: str | None = None
    feedback: str = ""
    round: int = 0


def house_proposals(role: str, universe: list[str], summary: dict) -> list[Proposal]:
    """Different reviewed signal families, with horizons chosen from development volatility."""
    vols = [v["annualized_vol"] for v in summary["symbols"].values() if v.get("annualized_vol") is not None]
    noisy = bool(vols and sum(vols) / len(vols) > 0.25)
    n = 100 if noisy else 60
    seeds = {
        "trend": {"fast": 35, "slow": 200},
        "breakout": {"lookback": n},
        "momentum": {"lookback": 200, "threshold": 0.01},
        "mean_reversion": {"lookback": n, "entry_z": 2.0, "exit_z": 0.0},
        "rsi": {"lookback": 30, "entry_rsi": 25, "exit_rsi": 65},
        "regime": {"regime_lookback": 200, "min_reversion": 0.025, "lookback": n, "sell_z": 1.5, "rebuy_z": 0.0},
        "defensive": {"vol_lookback": n, "trend_lookback": 400, "target_vol": 0.15},
        "rotation": {"lookback_sessions": 63, "top_k": 4, "rebalance_sessions": 5, "threshold": 0.0},
        "risk_rotation": {"lookback_sessions": 63, "top_k": 4, "rebalance_sessions": 5, "threshold": 0.0,
                          "vol_sessions": 21},
    }
    out = [Proposal(make_version(role, card, seeds[card], allocation_universe(universe)
                                 if CARDS[card].family in ALLOCATION_FAMILIES else universe),
                     "development-agent", CARDS[card].hypothesis,
                     feedback=f"development mean volatility {'above' if noisy else 'below'} 25%; horizon {n}")
            for card in ROLE_CARDS[role]]
    if role == "trend":
        out.append(Proposal(make_version(role, "rotation", {**seeds["rotation"], "lookback_sessions": 126},
                                         allocation_universe(universe)), "development-agent", CARDS["rotation"].hypothesis))
    if role == "defensive":
        out.append(Proposal(make_version(role, "risk_rotation", {**seeds["risk_rotation"], "rebalance_sessions": 1},
                                         allocation_universe(universe)), "development-agent", CARDS["risk_rotation"].hypothesis))
    return out


def validate_proposal(raw: dict, role: str, universe: list[str]) -> list[Proposal]:
    """Schema, family, universe and grid bounds checked before allocating any simulation."""
    prop = StrategyProposal.model_validate(raw)
    allowed = {CARDS[c].family for c in ROLE_CARDS[role]}
    expected = allocation_universe(universe) if prop.family in ALLOCATION_FAMILIES else universe
    if prop.family not in allowed or sorted(prop.universe) != expected or len(set(prop.universe)) != len(prop.universe):
        raise ValueError("proposal must use a reviewed role family and exactly the registered stock/ETF universe")
    bounds = FAMILIES[prop.family][1]
    if set(prop.param_grid) != set(bounds):
        raise ValueError("parameter grid keys must match the reviewed family")
    if any(not values or len(values) > 8 for values in prop.param_grid.values()):
        raise ValueError("each parameter needs 1..8 values")
    if math.prod(len(v) for v in prop.param_grid.values()) > 64:
        raise ValueError("proposal exceeds 64 parameter combinations")
    # Reject any out-of-bounds value, even if other combinations would be usable.
    # Relational constraints can legitimately invalidate only part of a bounded grid.
    for key, values in prop.param_grid.items():
        lo, hi = bounds[key]
        if any(not math.isfinite(v) or not lo <= v <= hi for v in values):
            raise ValueError(f"{key} has a value outside reviewed bounds [{lo}, {hi}]")
    out = []
    for values in itertools.product(*(prop.param_grid[k] for k in sorted(bounds))):
        params = dict(zip(sorted(bounds), values))
        try:
            validate_params(prop.family, params)
        except ValueError:  # e.g. fast >= slow in one cross-product, while others are valid
            continue
        out.append(Proposal(make_version(role, FAMILY_CARDS[prop.family.value], params, expected),
                            f"agent:{prop.agent_id}:{prop.proposal_id}", prop.rationale))
    if not out:
        raise ValueError("parameter grid contains no valid combination satisfying family constraints")
    return out


def mutations(parent: Proposal, metrics: dict, baseline: dict, round_no: int) -> list[Proposal]:
    """Critique measured weaknesses and propose bounded, deterministic local experiments."""
    v = parent.version
    params = v.param_dict
    if metrics["turnover_per_year"] > 20:
        feedback = "cost/churn: widen signal horizon or entry threshold"
        direction = 1
    elif metrics["max_drawdown"] > baseline["max_drawdown"]:
        feedback = "drawdown: test quicker exits or smaller volatility exposure"
        direction = -1
    else:
        feedback = "weak excess: test adjacent horizons and signal thresholds"
        direction = 1 if round_no % 2 else -1
    bounds = FAMILIES[v.family][1]
    out = []
    for key in sorted(params):
        p = dict(params)
        lo, hi = bounds[key]
        if key == "rebalance_sessions":
            p[key] = 1 if p[key] == 5 else 5
        elif key == "top_k":
            p[key] = min(hi, max(lo, p[key] + direction))
        elif key.endswith("lookback") or key.endswith("_sessions") or key in ("fast", "slow"):
            p[key] = min(hi, max(lo, round(p[key] * (1 + direction * 0.25))))
        else:
            step = (hi - lo) * 0.08
            p[key] = round(min(hi, max(lo, p[key] + direction * step)), 6)
        try:
            validate_params(v.family, p)
        except ValueError:
            continue
        child = make_version(v.strategy_id, FAMILY_CARDS[v.family.value], p, list(v.universe))
        if child.version != v.version:
            out.append(Proposal(child, "feedback-optimizer", parent.hypothesis, v.version, feedback, round_no))
    return out


def llm_proposals(role: str, universe: list[str], summary: dict, timeout_s: float) -> tuple[list[Proposal], dict]:
    """Optional request plus one schema repair inside one deadline; never called during execution."""
    from hellabandz.agents import llm
    if not llm.available():
        return [], {"role": role, "source": "offline", "reason": "no configured model key"}
    allowed = {CARDS[c].family.value: FAMILIES[CARDS[c].family][1] for c in ROLE_CARDS[role]}
    import json
    system = ("Propose a falsifiable long-only stock/ETF strategy using ONLY a reviewed family below. "
              "Do not promise profits. Respond as StrategyProposal JSON with proposal_id, agent_id, family, "
              "universe, param_grid, rationale and evidence. evidence MUST be an array of strings, not a string. "
              "Cross-parameter constraints: SMA fast < slow; RSI entry_rsi < exit_rsi; regime rebuy_z < sell_z. "
              "At most 8 values/parameter and 64 combinations. Example shape: "
              '{"proposal_id":"idea1","agent_id":"researcher","family":"ts_momentum",'
              '"universe":["SPY"],"param_grid":{"lookback":[100],"threshold":[0.01]},'
              '"rationale":"test trailing persistence","evidence":["development-only hypothesis"]}. '
              f"Use exactly universe {universe} for single-name/basket families. For relative_strength or "
              f"risk_adjusted_rotation use exactly {allocation_universe(universe)}. Allocation session parameters "
              "must be integers; rebalance_sessions only1 (daily) or5 (calendar weekly). "
              f"Families/bounds: {json.dumps(allowed)}")
    deadline = time.monotonic() + timeout_s
    user = json.dumps(summary)
    reason = "proposal timeout"
    errors = []
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= .1:
            break
        try:
            raw, meta = llm.chat_json(system, user, timeout=remaining, max_retries=0)
            proposals = validate_proposal(raw, role, universe)
            combinations = math.prod(len(v) for v in raw["param_grid"].values())
            return proposals, {"role": role, "source": "llm", **meta, "attempts": attempt + 1,
                               "proposal": raw, "valid_combinations": len(proposals),
                               "discarded_combinations": combinations - len(proposals), "repair_errors": errors}
        except ValueError as exc:
            reason = "schema or bounds invalid"
            errors.append(str(exc)[:300])
            user = json.dumps(summary) + f"\nRepair the previous proposal: {str(exc)[:300]}"
        except Exception as exc:
            reason = f"provider unavailable ({type(exc).__name__})"
            break
    return [], {"role": role, "source": "offline-fallback", "reason": reason, "repair_errors": errors}
