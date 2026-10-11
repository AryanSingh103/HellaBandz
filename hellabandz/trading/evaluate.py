"""Held-out evaluation of the coordinated portfolio, and a research critic.

    uv run python -m hellabandz.trading.evaluate                       # committed fixture
    uv run python -m hellabandz.trading.evaluate --dataset data/snapshots/<id>
    uv run python -m hellabandz.trading.evaluate --seeds 101-110       # fresh synthetic markets

For each dataset: research on train/validation (hellabandz.trading.research), then one run of the held-out
period through the paper CLOB for:
  - cash (no trading)
  - the passive core alone (the baseline: equal-weight buy-and-hold through the same venue, fees and spread)
  - each challenger alone, funded or not
  - the coordinated portfolio (champions at their weights + the core)
Reported: net return, max drawdown, Sharpe (hourly bars, 252x7 per year, no risk-free rate), turnover, fees,
slippage, mean exposure, fill and rejection counts, and the same split over three sub-periods with a regime
label. The held-out view is counted in the registry. Results are reported as they come out.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from pathlib import Path

import numpy as np

from hellabandz.backtester import SnapshotProvider
from hellabandz.ingestion.dataset import load_snapshot, write_snapshot
from hellabandz.strategies import BARS_PER_YEAR
from hellabandz.trading.execution import VenueMode
from hellabandz.trading.registry import Registry
from hellabandz.trading.research import (DEPLOYABLE, INITIAL, POLICY_VERSION, bar_rets, core_version, portfolio_sleeves, research, ret,
                                         simulate)
from hellabandz.trading.strategies import CODE_VERSION, FAMILY_CARDS, make_version
from hellabandz.trading.diagnostics import relative_attribution

ROOT = Path(__file__).resolve().parents[2]


def metrics(r, label: str) -> dict:
    e = np.asarray([r.initial_cash] + r.equity, dtype=np.float64)
    rr = np.diff(e) / e[:-1]
    peak = np.maximum.accumulate(e)
    sd = rr.std()
    downside = math.sqrt(float(np.mean(np.minimum(rr, 0) ** 2))) if len(rr) else 0.0
    submitted = len(r.orders)
    return {"label": label, "net_return": round(ret(r), 6), "max_drawdown": round(float(((peak - e) / peak).max()), 6),
            "sharpe": round(float(rr.mean() / sd * math.sqrt(BARS_PER_YEAR)), 3) if sd > 0 else 0.0,
            "sortino": round(float(rr.mean() / max(downside, 1e-12) * math.sqrt(BARS_PER_YEAR)), 3) if len(rr) else 0.0,
            "turnover_x_capital": round(r.turnover, 3), "fees": r.fees / 1e6, "slippage": r.slippage / 1e6,
            "mean_exposure_pct": round(r.exposure_bps_mean / 100, 1), "orders": submitted, "fills": r.fills,
            "ledger_rejections": r.ledger_rejections, "fill_rate": round(r.fills / submitted, 3) if submitted else None,
            "intent_outcomes": r.intent_outcomes, "ledger_digest": r.ledger_digest,
            "timings": r.timings, "cpu_s": r.cpu_s, "peak_rss_mb": r.peak_rss_mb}


def price_index_metrics(provider: SnapshotProvider, symbol: str, start: int, end: int) -> dict:
    """Report-only fully invested close-price reference; no execution/cost claim."""
    series, ts = provider.history(symbol, end)
    prices = series.close[ts >= start]
    if len(prices) < 2:
        raise ValueError("price-index comparator needs at least two test closes")
    rr = np.diff(prices) / prices[:-1]
    sd = float(rr.std())
    downside = math.sqrt(float(np.mean(np.minimum(rr, 0) ** 2)))
    peak = np.maximum.accumulate(prices)
    return {"label": f"{symbol} fully invested close-price index (report only, no execution costs)",
            "net_return": round(float(prices[-1] / prices[0] - 1), 6),
            "sharpe": round(float(rr.mean() / sd * math.sqrt(BARS_PER_YEAR)), 3) if sd > 0 else 0.0,
            "sortino": round(float(rr.mean() / max(downside, 1e-12) * math.sqrt(BARS_PER_YEAR)), 3),
            "max_drawdown": round(float(((peak - prices) / peak).max()), 6),
            "mean_exposure_pct": 100.0, "turnover_x_capital": 0.0, "fees": 0.0, "slippage": 0.0,
            "methodology": "first test close to last test close; dataset adjustment convention; no spread, fees, "
                           "integer shares, cash reserve or coordinator limits; turnover/execution costs not modeled",
            "first_close_ts": int(ts[ts >= start][0]), "last_close_ts": int(ts[-1]), "bars": len(prices)}


def regime_label(p: SnapshotProvider, universe: list[str], a: int, b: int) -> str:
    observations = []
    for s in universe:
        h, ts = p.history(s, b)
        m = ts >= a
        if m.sum() > 2:
            observations.append((ts[m], h.close[m]))
    if not observations:
        return "unknown"
    common = observations[0][0]
    for ts, _ in observations[1:]:
        common = np.intersect1d(common, ts)
    if len(common) < 3:
        return "unknown (insufficient common observations)"
    closes = [px[np.searchsorted(ts, common)] / px[np.searchsorted(ts, common[0])]
              for ts, px in observations]
    idx = np.mean(closes, axis=0)
    r = np.diff(np.log(idx))
    vol = r.std() * math.sqrt(BARS_PER_YEAR)
    return f"{'up' if idx[-1] > 1 else 'down'} {idx[-1] - 1:+.1%}, {'high' if vol > 0.25 else 'normal'} vol {vol:.0%}"


def validate_frozen_registry(reg: Registry, meta: dict, start: int) -> dict:
    found = reg.data.get("research_state", {})
    if found.get("status") != "complete" or found.get("policy_version") != POLICY_VERSION or found.get("code_version") != CODE_VERSION:
        raise ValueError("frozen evaluation requires completed current-policy/current-code research")
    if found.get("universe") != sorted(meta["symbols"]) or meta.get("interval_s") != 3600:
        raise ValueError("frozen registry universe and hourly data convention must match evaluation")
    convention = found.get("data_convention")
    if convention is not None and convention != {k: meta.get(k) for k in convention}:
        raise ValueError("frozen registry price adjustments, schema and accounting units must match evaluation")
    if start < found["splits"]["validation"][1]:
        raise ValueError("frozen evaluation must start after the registry's validation period")
    records = list(found.get("frozen_challengers", {}).values())
    records += [reg.data["versions"][c["key"]] for c in reg.data["champions"].values()]
    for rec in records:
        v = make_version(rec["strategy_id"], FAMILY_CARDS[rec["family"]], rec["params"], rec["universe"])
        if rec.get("code_version") != CODE_VERSION or rec["version"] != v.version:
            raise ValueError("frozen strategy version or runtime code has changed; refusing silent refit")
    for rec in found.get("roles", []):
        v = make_version(rec["role"], FAMILY_CARDS[rec["family"]], rec["params"], rec.get("universe", found["universe"]))
        frozen = found.get("frozen_challengers", {}).get(rec["role"], {})
        if rec["version"] != v.version or frozen.get("version") != v.version:
            raise ValueError("reported challenger differs from frozen strategy version")
    return found


def evaluate_dataset(dataset: Path, registry_path: Path, budget_s: float = 900.0, *, frozen_registry: bool = False,
                     start: int | None = None, end: int | None = None) -> dict:
    reg = Registry(registry_path)
    t, meta = load_snapshot(dataset)
    p = SnapshotProvider(t)
    te0, te1 = meta["splits"]["test"]
    if start is not None or end is not None:
        if not frozen_registry:
            raise ValueError("custom evaluation dates require --frozen-registry")
        te0, te1 = start if start is not None else te0, end if end is not None else te1
    if not te0 < te1 or te0 < int(t.ts.min()) or te1 > int(t.ts.max()) + meta["interval_s"]:
        raise ValueError("evaluation range must be nonempty and within the snapshot")
    found = validate_frozen_registry(reg, meta, te0) if frozen_registry else research(dataset, reg, budget_s)
    universe = sorted(meta["symbols"])
    views = reg.view_holdout(meta["content_sha256"])
    rows = {}
    cash = {"label": "cash", "net_return": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "turnover_x_capital": 0.0}
    core = simulate(p, meta, [("core", core_version(universe), DEPLOYABLE)], VenueMode.PAPER_CLOB, te0, te1, "test-core",
                    keep_telemetry=True)
    rows["baseline_passive_core"] = metrics(core, "passive core (baseline)")
    rows["baseline_passive_core"]["return_attribution"] = core.return_attribution
    if "SPY" in universe:
        spy = simulate(p, meta, [("core", core_version(["SPY"]), DEPLOYABLE)], VenueMode.PAPER_CLOB,
                       te0, te1, "test-spy")
        rows["baseline_spy"] = metrics(spy, "SPY holding under coordinator concentration/cash limits (report only)")
        rows["baseline_spy_price_index"] = price_index_metrics(p, "SPY", te0, te1)
    for role in found["roles"]:
        if role["version"] is None:
            continue
        card = FAMILY_CARDS[role["family"]]
        v = make_version(role["role"], card, role["params"], role.get("universe", universe))
        r = simulate(p, meta, [(role["role"], v, DEPLOYABLE)], VenueMode.PAPER_CLOB, te0, te1, f"test-{role['role']}",
                     keep_telemetry=True)
        rows[f"alone:{role['role']}"] = {**metrics(r, f"{role['role']} alone ({role['family']})"),
                                         "funded": role["accepted"], "weight": role["weight"],
                                         "return_attribution": r.return_attribution,
                                         "relative_attribution": relative_attribution(r, core)}
    sleeves = portfolio_sleeves(reg, universe)
    port = simulate(p, meta, sleeves, VenueMode.PAPER_CLOB, te0, te1, "test-portfolio", keep_decisions=True,
                    keep_telemetry=True)
    rows["coordinated_portfolio"] = metrics(port, "coordinated portfolio")
    rows["coordinated_portfolio"].update(return_attribution=port.return_attribution,
                                        relative_attribution=relative_attribution(port, core))
    # sub-periods: the same runs, split in three by time
    edges = [te0 + (te1 - te0) * k // 3 for k in range(4)]
    periods = []
    for a, b in zip(edges[:-1], edges[1:]):
        def part(r):
            idx = [i for i, ts in enumerate(r.equity_ts) if a <= ts < b]
            if not idx:
                return 0.0
            start = r.equity[idx[0] - 1] if idx[0] > 0 else r.initial_cash
            return r.equity[idx[-1]] / start - 1
        periods.append({"from": a, "to": b, "regime": regime_label(p, universe, a, b),
                        "portfolio": round(part(port), 6), "baseline": round(part(core), 6),
                        "excess": round(part(port) - part(core), 6)})
    out = {"dataset_id": meta["dataset_id"], "dataset_sha256": meta["content_sha256"], "holdout_views": views,
           "holdout_untouched": views == 1 and not meta.get("historical_periods_previously_inspected", False),
           "evaluation_mode": "frozen_registry_no_refit" if frozen_registry else "research_then_evaluate",
           "evaluation_range": [te0, te1],
           "historical_periods_previously_inspected": bool(meta.get("historical_periods_previously_inspected", False)),
           "research": found, "sleeves": [(s, v.version, c / 1e6) for s, v, c in sleeves],
           "rows": {"cash": cash, **rows}, "excess_vs_baseline": round(ret(port) - ret(core), 6),
           "sharpe_diff": round(rows["coordinated_portfolio"]["sharpe"] - rows["baseline_passive_core"]["sharpe"], 3),
           "periods": periods,
           "example_decisions": [d["explanation"] for d in port.decisions if d["order"]][:5]}
    out["critic"] = critic(out)
    return out


def critic(rep: dict) -> list[str]:
    """Research critic. Reads results, names weaknesses, proposes experiments. Never trades."""
    f = []
    if not rep["holdout_untouched"]:
        f.append(f"Same-dataset evaluations: {rep['holdout_views']}; reused/previously inspected historical evidence "
                 "is exploratory, not an untouched test.")
    for role in rep["research"]["roles"]:
        if not role["accepted"]:
            f.append(f"{role['role']} was not funded ({', '.join(role['reasons'])}). Experiment: a different family or "
                     f"a longer horizon for this role, judged on validation only.")
        elif role["metrics"].get("validation_turnover_per_year", 0) > 6:
            f.append(f"{role['role']} trades {role['metrics']['validation_turnover_per_year']}x capital a year; "
                     "a wider rebalance band may keep the edge at lower cost.")
    ex = rep["excess_vs_baseline"]
    rows = rep["rows"]
    if ex == 0:
        f.append("Nothing was funded, so the portfolio held the passive core and matched the baseline exactly.")
    elif ex < 0:
        f.append(f"The coordinated portfolio trailed the passive core by {ex:+.2%} on the held-out period.")
    else:
        f.append(f"The portfolio beat the passive core by {ex:+.2%} on one held-out period. One period is weak evidence; "
                 "check it across independent markets (--seeds) before believing it.")
    if rows["coordinated_portfolio"]["max_drawdown"] > rows["baseline_passive_core"]["max_drawdown"]:
        f.append("Drawdown was deeper than the baseline's.")
    wins = sum(p["excess"] > 0 for p in rep["periods"])
    f.append(f"Ahead of the baseline in {wins} of {len(rep['periods'])} sub-periods: "
             + "; ".join(f"{p['regime']}: {p['excess']:+.2%}" for p in rep["periods"]))
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "data/fixtures/synthetic_1h"))
    ap.add_argument("--seeds", help="e.g. 101-110: evaluate on fresh synthetic markets instead")
    ap.add_argument("--registry")
    ap.add_argument("--budget-s", type=float, default=900)
    ap.add_argument("--frozen-registry", action="store_true", help="evaluate frozen versions without any research/refit")
    ap.add_argument("--start", type=int, help="inclusive epoch seconds, frozen evaluation only")
    ap.add_argument("--end", type=int, help="exclusive epoch seconds, frozen evaluation only")
    ap.add_argument("--json")
    a = ap.parse_args()
    if a.frozen_registry and (not a.registry or a.seeds):
        ap.error("--frozen-registry requires --registry and a concrete --dataset")
    reports = []
    if a.seeds:
        from hellabandz.ingestion.synthetic import generate
        lo, hi = (int(x) for x in a.seeds.split("-"))
        for seed in range(lo, hi + 1):
            with tempfile.TemporaryDirectory() as d:
                snap = Path(d) / "snap"
                write_snapshot(generate(seed=seed), snap, {"dataset_id": f"synthetic-1h-seed{seed}", "interval_s": 3600,
                                                           "source": f"hellabandz.ingestion.synthetic (seed={seed})"})
                rep = evaluate_dataset(snap, Path(d) / "registry.json", a.budget_s)
                reports.append(rep)
                print(f"seed {seed}: portfolio {rep['rows']['coordinated_portfolio']['net_return']:+.2%} baseline "
                      f"{rep['rows']['baseline_passive_core']['net_return']:+.2%} excess {rep['excess_vs_baseline']:+.2%} "
                      f"funded {[r['role'] for r in rep['research']['roles'] if r['accepted']]}", flush=True)
        ex = np.array([r["excess_vs_baseline"] for r in reports])
        summary = {"markets": len(ex), "mean_excess": round(float(ex.mean()), 6),
                   "standard_error": round(float(ex.std(ddof=1) / math.sqrt(len(ex))), 6) if len(ex) > 1 else None,
                   "ahead_in": int((ex > 0).sum()), "equal_in": int((ex == 0).sum())}
        print(json.dumps(summary))
        out = {"summary": summary, "reports": reports}
    else:
        reg_path = Path(a.registry) if a.registry else Path(tempfile.mkdtemp()) / "registry.json"
        out = evaluate_dataset(Path(a.dataset), reg_path, a.budget_s, frozen_registry=a.frozen_registry,
                               start=a.start, end=a.end)
        print(json.dumps({k: out[k] for k in ("dataset_id", "excess_vs_baseline", "sharpe_diff", "holdout_views")}))
        for k, r in out["rows"].items():
            print(f"  {k:28} return {r['net_return']:+.2%}  maxDD {r['max_drawdown']:.2%}  sharpe {r['sharpe']}  "
                  f"turnover {r['turnover_x_capital']}x")
        for line in out["critic"]:
            print("  critic:", line)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
