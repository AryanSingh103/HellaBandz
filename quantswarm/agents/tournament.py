"""Strategy tournament orchestrated with LangGraph.

    summarize(train only) -> momentum | mean_reversion | volatility   (LLM, parallel)
        -> fit (deterministic grid search on train)
        -> rank (deterministic validation backtests + stability folds + gates)
        -> supervisor (LLM suggests weights, bounded)
        -> allocate (deterministic) -> held_out_test (evaluated once, after freeze)
        -> manifest (hashed, saved, optionally published)

LLMs only produce schema-validated JSON (strategy family + parameter grid,
or allocation suggestions). Everything that decides money is deterministic.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import operator
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from quantswarm.agents import llm
from quantswarm.agents.research import train_summary
from quantswarm.backtester import SnapshotProvider, Slot, run_backtest
from quantswarm.contracts import MICROS, StrategyFamily, StrategyProposal, StrategySpec
from quantswarm.ingestion.dataset import load_snapshot
from quantswarm.ledger import EngineConfig, canonical, sha256_hex
from quantswarm.strategies import FAMILIES, validate_params

log = logging.getLogger("tournament")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "data/fixtures/synthetic_1h"
INITIAL_CASH = 10_000 * MICROS
EVAL_CAPITAL = 9_000 * MICROS  # each candidate is evaluated alone with the deployable 90%
MAX_COMBOS = 48
MIN_FILLS = 10
MAX_DD = 0.25
CAP_PER_STRATEGY = 0.40
DEPLOYABLE = 0.90
SUPERVISOR_BAND = 0.20

AGENTS = {
    "momentum": {"families": [StrategyFamily.SMA_CROSSOVER],
                 "brief": "trend-following researcher (moving-average crossovers)"},
    "breakout": {"families": [StrategyFamily.BREAKOUT],
                 "brief": "breakout researcher (buy closes above the prior N-bar high, exit below the recent low)"},
    "mean_reversion": {"families": [StrategyFamily.ZSCORE_REVERSION],
                       "brief": "mean-reversion researcher (rolling z-score deviations)"},
    "volatility": {"families": [StrategyFamily.VOL_TARGET],
                   "brief": "volatility-regime researcher (exposure scaled to realized volatility with a trend filter)"},
    "rsi": {"families": [StrategyFamily.RSI_REVERSION],
            "brief": "oversold-bounce researcher (buy when RSI is low, exit once it recovers)"},
    "trend": {"families": [StrategyFamily.TS_MOMENTUM],
              "brief": "time-series momentum researcher (hold while the trailing N-bar return beats a threshold)"},
}


# ----------------------------------------------------------------------------- deterministic pieces

_provider: SnapshotProvider | None = None


def _init_worker(dataset_dir: str) -> None:
    global _provider
    _provider = SnapshotProvider(load_snapshot(Path(dataset_dir))[0])


def _bt(args: tuple) -> dict:
    spec_json, start, end = args
    spec = StrategySpec.model_validate_json(spec_json)
    r = run_backtest(_provider, [Slot(spec, EVAL_CAPITAL)], start, end, initial_cash=INITIAL_CASH,
                     run_id=spec.strategy_id)
    return {"strategy_id": spec.strategy_id, "start": start, "end": end, **r.summary()}


def expand_grid(prop: StrategyProposal) -> list[dict[str, float]]:
    bounds = FAMILIES[prop.family][1]
    keys = sorted(bounds)
    grid = [sorted(set(prop.param_grid.get(k, []))) for k in keys]
    combos = []
    for vals in itertools.product(*grid):
        params = dict(zip(keys, vals))
        try:
            validate_params(prop.family, params)
            combos.append(params)
        except ValueError:
            continue
    return combos[:MAX_COMBOS]


def train_objective(m: dict) -> float:
    if m["n_fills"] < MIN_FILLS:
        return -1e9  # finite sentinel: manifests must be strict JSON (Postgres JSONB rejects Infinity)
    return m["sharpe"] * (1 - m["max_drawdown"])


def score(val: dict, stability: float) -> tuple[float, list[str]]:
    gates = []
    if val["n_fills"] < MIN_FILLS:
        gates.append(f"n_fills {val['n_fills']} < {MIN_FILLS}")
    if val["max_drawdown"] > MAX_DD:
        gates.append(f"max_drawdown {val['max_drawdown']:.3f} > {MAX_DD}")
    s = val["sharpe"] * (1 - val["max_drawdown"]) * stability
    if gates or s <= 0:
        return 0.0, gates or ["non-positive risk-adjusted score"]
    return round(s, 6), []


def allocate(scores: dict[str, float], supervisor: dict[str, float] | None) -> tuple[dict[str, float], dict]:
    """Deterministic allocator. Supervisor can only move each weight within +/- SUPERVISOR_BAND
    of the score-proportional weight; caps and the cash reserve always apply."""
    total = sum(scores.values())
    base = {k: (v / total if total > 0 else 0.0) for k, v in scores.items()}
    blended = {}
    for k, b in base.items():
        if b == 0:
            blended[k] = 0.0
            continue
        sup = (supervisor or {}).get(k, b)
        sup = min(max(sup, b - SUPERVISOR_BAND), b + SUPERVISOR_BAND)
        blended[k] = max(0.0, 0.5 * b + 0.5 * sup)
    s = sum(blended.values())
    w = {k: (v / s if s > 0 else 0.0) * DEPLOYABLE for k, v in blended.items()}
    w = {k: round(min(v, CAP_PER_STRATEGY), 6) for k, v in w.items()}
    return w, {"score_weights": base, "blended": blended}


# ----------------------------------------------------------------------------- LLM pieces

SYSTEM_RESEARCHER = """You are the {agent} agent in a quantitative research team: a {brief}.
You may ONLY choose from these whitelisted strategy families and parameter bounds:
{families}
You see summary statistics for the TRAINING period only. Propose one strategy family, a universe
(subset of the available symbols), and a small parameter grid (2-4 candidate values per parameter,
inside the bounds). A deterministic optimizer will pick the final values on training data and a
separate validation period will judge you. Respond with JSON:
{{"family": str, "universe": [str], "param_grid": {{param: [numbers]}}, "rationale": str (<= 80 words),
"evidence": [short strings citing the statistics you used]}}"""

SYSTEM_SUPERVISOR = """You are the portfolio supervisor. Given validation-period results for competing
strategies, suggest capital weights (fractions summing to <= 1) and a one-paragraph rationale. Weights
are advisory: a deterministic allocator bounds them within +/-20 percentage points of score-proportional
weights, caps each strategy at 40% and keeps a 10% cash reserve. Strategies with score 0 get nothing.
Respond with JSON: {"weights": {strategy_id: number}, "rationale": str}"""


def research(agent_id: str, summary: dict, run_id: str) -> dict:
    """Ask the LLM for a proposal (one retry with the validation error), else use the recorded fixture."""
    cfg = AGENTS[agent_id]
    symbols = sorted(summary["symbols"])
    fam_desc = {f.value: FAMILIES[f][1] for f in cfg["families"]}
    record: dict[str, Any] = {"agent_id": agent_id}
    prop = None
    if llm.available():
        user = json.dumps(summary)
        sys_prompt = SYSTEM_RESEARCHER.format(agent=agent_id, brief=cfg["brief"], families=json.dumps(fam_desc))
        for attempt in range(2):
            try:
                raw, meta = llm.chat_json(sys_prompt, user)
                prop = _to_proposal(agent_id, raw, run_id, cfg["families"], symbols)
                record.update(source="llm", llm=meta, attempts=attempt + 1)
                break
            except (ValidationError, ValueError, KeyError, TypeError) as e:  # bad answer: retry once
                user = json.dumps(summary) + f"\nYour previous answer was invalid: {str(e)[:400]}. Fix it."
                record.setdefault("errors", []).append(str(e)[:400])
            except Exception as e:  # network / auth / quota: no point retrying
                record.setdefault("errors", []).append(f"llm unavailable: {type(e).__name__}: {str(e)[:200]}")
                break
    if prop is None:
        raw = json.loads((ROOT / f"data/fixtures/proposals/{agent_id}.json").read_text())
        # recorded proposals name the default equity universe; keep what this dataset has
        raw["universe"] = [u for u in raw["universe"] if u in symbols] or symbols[:3]
        prop = _to_proposal(agent_id, raw, run_id, cfg["families"], symbols)
        record["source"] = "fixture"
    record["proposal"] = prop.model_dump(mode="json")
    return record


def _to_proposal(agent_id: str, raw: dict, run_id: str, allowed: list[StrategyFamily],
                 symbols: list[str]) -> StrategyProposal:
    if not isinstance(raw, dict):
        raise TypeError("expected a JSON object")
    universe = sorted(set(raw["universe"]))
    unknown = [u for u in universe if u not in symbols]
    if unknown:
        raise ValueError(f"unknown symbols {unknown}; choose from {symbols}")
    prop = StrategyProposal(proposal_id=f"{run_id}.{agent_id}", agent_id=agent_id, family=raw["family"],
                            universe=universe, param_grid=raw["param_grid"],
                            rationale=str(raw.get("rationale", ""))[:2000],
                            evidence=[str(e)[:200] for e in raw.get("evidence", [])][:10])
    if prop.family not in allowed:
        raise ValueError(f"family {prop.family} not allowed for {agent_id}; choose from {[f.value for f in allowed]}")
    if not expand_grid(prop):
        raise ValueError("parameter grid has no valid combination inside the bounds")
    return prop


def supervise(results: list[dict]) -> dict:
    if llm.available():
        try:
            raw, meta = llm.chat_json(SYSTEM_SUPERVISOR, json.dumps(results))
            w = {k: float(v) for k, v in raw.get("weights", {}).items() if isinstance(v, (int, float))}
            return {"source": "llm", "llm": meta, "weights": w, "rationale": str(raw.get("rationale", ""))[:1500]}
        except Exception as e:
            err: str | None = f"{type(e).__name__}: {str(e)[:200]}"
    else:
        err = None
    tot = sum(r["score"] for r in results)
    w = {r["strategy_id"]: (r["score"] / tot if tot else 0.0) for r in results}
    best = max(results, key=lambda r: r["score"])
    rationale = (f"Deterministic fallback supervisor: weights proportional to validation score. "
                 f"Strongest candidate: {best['strategy_id']} (score {best['score']:.3f}). "
                 + " ".join(f"{r['strategy_id']} excluded ({'; '.join(r['gates'])})." for r in results if r['score'] == 0))
    return {"source": "fallback", "weights": w, "rationale": rationale, **({"error": err} if err else {})}


# ----------------------------------------------------------------------------- graph


class TState(TypedDict, total=False):
    run_id: str
    dataset_dir: str
    manifest_in: dict
    summary: dict
    proposals: Annotated[list[dict], operator.add]
    fitted: list[dict]
    ranked: list[dict]
    supervisor: dict
    allocations: dict
    test: dict
    manifest: dict


def build_graph(emit=lambda *a: None):
    def summarize(st: TState) -> TState:
        p = SnapshotProvider(load_snapshot(Path(st["dataset_dir"]))[0])
        summ = train_summary(p, st["manifest_in"]["splits"]["train"][1], st["manifest_in"])
        emit("system", "summary", summ)
        return {"summary": summ}

    def make_researcher(agent_id: str):
        def node(st: TState) -> TState:
            rec = research(agent_id, st["summary"], st["run_id"])
            emit(agent_id, "proposal", rec)
            return {"proposals": [rec]}
        return node

    def fit(st: TState) -> TState:
        tr = st["manifest_in"]["splits"]["train"]
        jobs, owners = [], []
        for rec in sorted(st["proposals"], key=lambda r: r["agent_id"]):
            prop = StrategyProposal.model_validate(rec["proposal"])
            for i, params in enumerate(expand_grid(prop)):
                spec = StrategySpec(strategy_id=f"{prop.agent_id}.c{i:02d}", agent_id=prop.agent_id,
                                    family=prop.family, params=params, universe=sorted(prop.universe))
                jobs.append((spec.model_dump_json(), tr[0], tr[1]))
                owners.append((prop.agent_id, spec))
        results = _pool_map(st["dataset_dir"], jobs)
        best: dict[str, tuple[float, StrategySpec, dict]] = {}
        for (agent, spec), r in zip(owners, results):
            obj = train_objective(r["metrics"])
            if agent not in best or obj > best[agent][0]:
                best[agent] = (obj, spec, r)
        fitted = []
        for agent, (obj, spec, r) in sorted(best.items()):
            final = spec.model_copy(update={"strategy_id": f"{st['run_id']}.{agent}"})
            n = sum(1 for a, _ in owners if a == agent)
            fitted.append({"spec": final.model_dump(mode="json"), "train": r["metrics"], "train_objective": obj,
                           "candidates_tested": n})
            emit(agent, "fit", {"chosen_params": final.params, "candidates": n, "train_metrics": r["metrics"]})
        return {"fitted": fitted}

    def rank(st: TState) -> TState:
        sp = st["manifest_in"]["splits"]
        va = sp["validation"]
        folds_edges = [sp["train"][0] + (va[1] - sp["train"][0]) * k // 3 for k in range(4)]
        jobs = []
        for f in st["fitted"]:
            sj = json.dumps(f["spec"])
            jobs.append((sj, va[0], va[1]))
            jobs.extend((sj, folds_edges[k], folds_edges[k + 1]) for k in range(3))
        res = _pool_map(st["dataset_dir"], jobs)
        ranked = []
        for i, f in enumerate(st["fitted"]):
            val, folds = res[4 * i], res[4 * i + 1: 4 * i + 4]
            stability = sum(r["metrics"]["net_return"] > 0 for r in folds) / 3
            sc, gates = score(val["metrics"], stability)
            ranked.append({"strategy_id": f["spec"]["strategy_id"], "agent_id": f["spec"]["agent_id"],
                           "family": f["spec"]["family"], "params": f["spec"]["params"],
                           "train": f["train"], "validation": val["metrics"], "stability": stability,
                           "fold_returns": [r["metrics"]["net_return"] for r in folds],
                           "score": sc, "gates": gates, "validation_trades_digest": val["trades_digest"]})
            emit(f["spec"]["agent_id"], "validation", ranked[-1])
        return {"ranked": ranked}

    def supervisor(st: TState) -> TState:
        from quantswarm.ingestion.dataset import unit_context
        view = [{k: r[k] for k in ("strategy_id", "family", "params", "validation", "stability", "score", "gates")}
                for r in st["ranked"]]
        for result in view:
            result["units"] = unit_context(st["manifest_in"])
            result["legacy_usd_fields"] = "quote amounts; use units.quote_currency"
        sup = supervise(view)
        emit("supervisor", "allocation_advice", sup)
        return {"supervisor": sup}

    def allocate_node(st: TState) -> TState:
        scores = {r["strategy_id"]: r["score"] for r in st["ranked"]}
        w, detail = allocate(scores, st["supervisor"].get("weights"))
        allocs = {sid: {"weight": wt, "capital_micros": int(INITIAL_CASH * wt)} for sid, wt in w.items()}
        emit("allocator", "allocations", {"allocations": allocs, **detail})
        return {"allocations": {"by_strategy": allocs, **detail}}

    def held_out(st: TState) -> TState:
        """Evaluated exactly once, after specs and allocations are frozen."""
        te = st["manifest_in"]["splits"]["test"]
        p = SnapshotProvider(load_snapshot(Path(st["dataset_dir"]))[0])
        specs = {f["spec"]["strategy_id"]: StrategySpec.model_validate(f["spec"]) for f in st["fitted"]}
        slots = [Slot(specs[sid], a["capital_micros"]) for sid, a in st["allocations"]["by_strategy"].items()
                 if a["capital_micros"] > 0]
        port = run_backtest(p, slots, te[0], te[1], initial_cash=INITIAL_CASH, run_id=f"{st['run_id']}.test")
        per = {sid: run_backtest(p, [Slot(sp, EVAL_CAPITAL)], te[0], te[1], initial_cash=INITIAL_CASH,
                                 run_id=f"{sid}.test").metrics.model_dump() for sid, sp in specs.items()}
        test = {"portfolio": port.summary(), "per_strategy_standalone": per,
                "equity_curve": {"ts": port.equity_ts[::7], "equity": port.equity[::7]}}
        emit("system", "held_out_test", {"portfolio": port.metrics.model_dump(), "per_strategy": per})
        return {"test": test}

    def manifest(st: TState) -> TState:
        m = {
            "run_id": st["run_id"],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "code_version": _git_sha(),
            "dataset": {k: st["manifest_in"][k] for k in ("dataset_id", "content_sha256", "splits", "source",
                        "interval_s", "unit_version", "quote_currency", "lots_per_base_unit", "base_units_per_lot")
                        if k in st["manifest_in"]},
            "engine_config": EngineConfig().__dict__, "engine_config_digest": EngineConfig().digest(),
            "policy": {"min_fills": MIN_FILLS, "max_dd": MAX_DD, "cap_per_strategy": CAP_PER_STRATEGY,
                       "deployable": DEPLOYABLE, "supervisor_band": SUPERVISOR_BAND, "initial_cash_micros": INITIAL_CASH},
            "proposals": sorted(st["proposals"], key=lambda r: r["agent_id"]),
            "fitted": st["fitted"], "ranked": st["ranked"], "supervisor": st["supervisor"],
            "allocations": st["allocations"], "held_out_test": st["test"],
        }
        return {"manifest": m}

    g = StateGraph(TState)
    g.add_node("summarize", summarize)
    for a in AGENTS:
        g.add_node(a, make_researcher(a))
        g.add_edge("summarize", a)
    for name, fn in [("fit", fit), ("rank", rank), ("supervisor", supervisor), ("allocate", allocate_node),
                     ("held_out_test", held_out), ("manifest", manifest)]:
        g.add_node(name, fn)
    g.add_edge(START, "summarize")
    g.add_edge(list(AGENTS), "fit")
    g.add_edge("fit", "rank")
    g.add_edge("rank", "supervisor")
    g.add_edge("supervisor", "allocate")
    g.add_edge("allocate", "held_out_test")
    g.add_edge("held_out_test", "manifest")
    g.add_edge("manifest", END)
    return g.compile()


def _pool_map(dataset_dir: str, jobs: list) -> list[dict]:
    workers = int(os.environ.get("QS_TOURNAMENT_PROCS", str(min(4, os.cpu_count() or 1))))
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(dataset_dir,)) as ex:
        return list(ex.map(_bt, jobs, chunksize=2))


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def manifest_hash(m: dict) -> str:
    return sha256_hex(canonical(m))


def run(dataset_dir: Path = DEFAULT_DATASET, run_id: str | None = None, api: str | None = None,
        progress=lambda *args: None) -> dict:
    _, dm = load_snapshot(dataset_dir)
    run_id = run_id or "T" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    msgs: list[tuple] = []

    def emit(agent: str, role: str, content: Any) -> None:
        msgs.append((agent, role, content))
        log.info("[%s] %s", agent, role)
        progress(agent, role, content)
        if api:
            import httpx
            try:
                httpx.post(f"{api}/v1/agent-messages", json={"run_id": run_id, "agent_id": agent, "role": role,
                                                              "content": json.loads(json.dumps(content, default=str))},
                           timeout=10)
            except Exception:
                log.warning("could not publish agent message to API")

    out = build_graph(emit).invoke({"run_id": run_id, "dataset_dir": str(dataset_dir), "manifest_in": dm,
                                    "proposals": []})
    m = out["manifest"]
    h = manifest_hash(m)
    from quantswarm.ingestion.registry import MANIFESTS
    out_dir = MANIFESTS
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{run_id}.json").write_text(json.dumps(m, indent=1, sort_keys=True))
    if api:
        import httpx
        httpx.post(f"{api}/v1/tournaments", json={"run_id": run_id, "manifest": m, "manifest_hash": h}, timeout=30).raise_for_status()
    return {"run_id": run_id, "manifest_hash": h, "manifest": m}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--run-id")
    ap.add_argument("--api", default=os.environ.get("QS_API"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    r = run(Path(a.dataset), a.run_id, a.api)
    m = r["manifest"]
    print(f"\nTournament {r['run_id']}  manifest sha256={r['manifest_hash']}")
    print(f"{'strategy':32} {'src':8} {'train SR':>8} {'val SR':>8} {'val DD':>7} {'stab':>5} {'score':>7} {'weight':>7}")
    for rk in m["ranked"]:
        src = next(p["source"] for p in m["proposals"] if p["agent_id"] == rk["agent_id"])
        w = m["allocations"]["by_strategy"][rk["strategy_id"]]["weight"]
        print(f"{rk['strategy_id']:32} {src:8} {rk['train']['sharpe']:8.2f} {rk['validation']['sharpe']:8.2f} "
              f"{rk['validation']['max_drawdown']:7.3f} {rk['stability']:5.2f} {rk['score']:7.3f} {w:7.2%}")
    t = m["held_out_test"]["portfolio"]["metrics"]
    print(f"held-out test (evaluated once): return {t['net_return']:.2%}  sharpe {t['sharpe']:.2f}  "
          f"max DD {t['max_drawdown']:.2%}  fills {t['n_fills']}")


if __name__ == "__main__":
    main()
