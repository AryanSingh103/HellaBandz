"""Derived equal-weight buy-and-hold evidence, kept outside finalized manifests."""
from __future__ import annotations

import json
from itertools import groupby
from pathlib import Path

from quantswarm.backtester import SnapshotProvider
from quantswarm.contracts import ApplyBar, OpenPortfolio, OrderRequest, SetAllocation, SubmitOrder
from quantswarm.ingestion.dataset import load_snapshot
from quantswarm.ingestion.registry import MANIFESTS
from quantswarm.ledger import EngineConfig, PortfolioState, canonical, process, sha256_hex


def save_analytics(manifest: dict, dataset_path: Path):
    table, meta = load_snapshot(dataset_path)
    if meta["content_sha256"] != manifest["dataset"]["content_sha256"]:
        raise ValueError("baseline snapshot hash does not match frozen manifest")
    provider = SnapshotProvider(table)
    start, end = manifest["dataset"]["splits"]["test"]
    capital = manifest["policy"]["initial_cash_micros"]
    state, cfg = PortfolioState(), EngineConfig()
    process(state, OpenPortfolio(initial_cash=capital), cfg)
    process(state, SetAllocation(agent_id="baseline", capital=capital), cfg)
    points = []
    symbols = provider.symbols()
    for index, (ts, bars) in enumerate(groupby(provider.bars(start, end), key=lambda b: b.ts)):
        bars = list(bars)
        for bar in bars:
            process(state, ApplyBar(bar=bar), cfg)
        if index == 0:
            for bar in bars:
                ref = (bar.close * (10000 + cfg.reserve_buffer_bps) + 9999) // 10000
                qty = (capital * cfg.max_gross_bps // 10000 // len(symbols)) // (ref + cfg.fee_per_share)
                chunk = max(1, capital * cfg.max_order_bps // 10000 // ref)
                part = 0
                while qty > 0:
                    q = min(qty, chunk)
                    process(state, SubmitOrder(order=OrderRequest(
                        client_order_id=f"baseline.{bar.symbol}.{part}", agent_id="baseline", symbol=bar.symbol,
                        side="BUY", qty=q, signal_bar_ts=ts)), cfg)
                    qty -= q
                    part += 1
        points.append({"ts": int(ts.timestamp()), "equity": state.equity()})
    stride = max(1, len(points) // 1000)
    sampled = points[::stride]
    if points and sampled[-1] != points[-1]:
        sampled.append(points[-1])
    artifact = {"status": "available", "run_id": manifest["run_id"], "manifest_hash": sha256_hex(canonical(manifest)),
                "dataset_hash": meta["content_sha256"], "start_ts": start, "end_ts": end,
                "initial_capital_micros": capital, "quote_currency": meta.get("quote_currency", "USD"),
                "baseline": sampled, "strategy": manifest["held_out_test"]["equity_curve"],
                "assumptions": "Equal target weights across all frozen dataset symbols; 90% gross target and cash buffer. "
                               "Orders decided after first evaluation bar, filled on later bars with the same 1% participation, "
                               "fees, slippage, reservation limits and 5-bar expiry as strategies. Unfilled lots remain cash; "
                               "no rebalancing or forced final liquidation. Full held-out research period, without session crash overlay.",
                "config": cfg.__dict__, "state_digest": state.digest()}
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    path = MANIFESTS / f"{manifest['run_id']}.analytics.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(artifact, sort_keys=True))
    temp.replace(path)
    return artifact
