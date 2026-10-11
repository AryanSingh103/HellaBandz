"""Derived equal-weight buy-and-hold evidence, kept outside finalized manifests."""
from __future__ import annotations

import json
from pathlib import Path

from quantswarm.backtester import BacktestResult, SnapshotProvider, Slot, run_backtest
from quantswarm.ingestion.dataset import load_snapshot
from quantswarm.ingestion.registry import MANIFESTS
from quantswarm.ledger import EngineConfig, canonical, sha256_hex


def simulate(provider: SnapshotProvider, start: int, end: int, capital: int,
             cfg: EngineConfig = EngineConfig()) -> BacktestResult:
    """Equal-weight buy-and-hold of every symbol with the deployable share of capital.

    This is the tournament's passive core run on its own, so the benchmark and the default home of
    unearned capital are one definition: a portfolio that funds no strategy matches it exactly."""
    from quantswarm.agents.tournament import core_spec
    return run_backtest(provider, [Slot(core_spec("baseline", provider.symbols()), capital * cfg.max_gross_bps // 10_000)],
                        start, end, cfg, initial_cash=capital, run_id="baseline")


def save_analytics(manifest: dict, dataset_path: Path):
    table, meta = load_snapshot(dataset_path)
    if meta["content_sha256"] != manifest["dataset"]["content_sha256"]:
        raise ValueError("baseline snapshot hash does not match frozen manifest")
    start, end = manifest["dataset"]["splits"]["test"]
    capital = manifest["policy"]["initial_cash_micros"]
    cfg = EngineConfig()
    result = simulate(SnapshotProvider(table), start, end, capital, cfg)
    points = [{"ts": ts, "equity": equity} for ts, equity in zip(result.equity_ts, result.equity)]
    stride = max(1, len(points) // 1000)
    sampled = points[::stride]
    if points and sampled[-1] != points[-1]:
        sampled.append(points[-1])
    artifact = {"status": "available", "run_id": manifest["run_id"], "manifest_hash": sha256_hex(canonical(manifest)),
                "dataset_hash": meta["content_sha256"], "start_ts": start, "end_ts": end,
                "initial_capital_micros": capital, "quote_currency": meta.get("quote_currency", "USD"),
                "baseline": sampled, "strategy": manifest["held_out_test"]["equity_curve"],
                # computed on every bar, with the same formulas as strategy metrics
                "baseline_metrics": result.metrics.model_dump(),
                "strategy_metrics": manifest["held_out_test"]["portfolio"]["metrics"],
                "assumptions": "Equal dollar slots across all frozen dataset symbols, 90% of capital in total and whole shares "
                               "only, so some cash stays idle. Orders decided after the first evaluation bar and filled on later "
                               "bars with the same 1% participation, fees, slippage, reservation limits and 5-bar expiry as "
                               "strategies. Positions are never trimmed, topped up or liquidated. This is the tournament's "
                               "passive core on its own. Full held-out research period, without session crash overlay.",
                "config": cfg.__dict__, "state_digest": result.state_digest}
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    path = MANIFESTS / f"{manifest['run_id']}.analytics.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(artifact, sort_keys=True))
    temp.replace(path)
    return artifact
