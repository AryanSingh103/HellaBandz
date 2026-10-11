"""Deterministic multi-agent demonstration and benchmark.

    uv run python -m hellabandz.trading.demo                     # both venue modes, prints a summary
    uv run python -m hellabandz.trading.demo --json out.json     # also writes the full result
    docker run --rm --cpus=2 --memory=1g ... python -m hellabandz.trading.demo --background-research

Runs three strategy agents over the held-out period of the committed synthetic fixture (no keys,
no network), with the coordinator, the execution authority and the in-process ledger. Running it
twice gives identical digests. With --background-research, a parameter search runs in a separate,
lower-priority process at the same time, so execution latency can be compared with and without it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from hellabandz.backtester import SnapshotProvider
from hellabandz.contracts import MICROS
from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.trading.coordinator import paper_budget
from hellabandz.trading.execution import VenueMode
from hellabandz.trading.instruments import instruments_for_dataset
from hellabandz.trading.marketdata import DataClass
from hellabandz.trading.runtime import CoordinatedRuntime
from hellabandz.trading.strategies import RuntimeStrategy, make_version

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "data/fixtures/synthetic_1h"
UNIVERSE = ["AAPL", "MSFT", "NVDA", "QQQ", "SPY"]
STRATEGIES = [("trend", "trend", {"fast": 20, "slow": 100}),
              ("mean_reversion", "mean_reversion", {"lookback": 30, "entry_z": 1.5, "exit_z": 0.0}),
              ("defensive", "defensive", {"vol_lookback": 50, "trend_lookback": 200, "target_vol": 0.12})]
CAPITAL = {"trend": 3000 * MICROS, "mean_reversion": 3000 * MICROS, "defensive": 3000 * MICROS}


def run(mode: VenueMode, cycles: int | None) -> dict:
    t, m = load_snapshot(FIXTURE)
    p = SnapshotProvider(t)
    venue = "bar_sim" if mode == VenueMode.HISTORICAL_SIM else "paper_clob"
    strats = [RuntimeStrategy(make_version(sid, role, params, UNIVERSE), venue) for sid, role, params in STRATEGIES]
    rt = CoordinatedRuntime(f"DEMO-{mode.value}", mode, strats, CAPITAL, paper_budget(10_000 * MICROS),
                            instruments_for_dataset(m), DataClass.SYNTHETIC, keep_decisions=True)
    rt.open()
    te = m["splits"]["test"]
    rt.warm(p, te[0])
    t0 = time.perf_counter()
    r = rt.run(p, te[0], te[1], max_cycles=cycles)
    wall = time.perf_counter() - t0
    multi = next((d for d in r.decisions if d["order"] and sum(1 for i in d["intents"]
                  if i["requested_qty"] != i["attributed_qty"]) >= 2), None)
    netted = next((d for d in r.decisions if d["transfers"]), None)
    return {"mode": r.mode, "cycles": r.cycles, "wall_s": round(wall, 3), "cycles_per_s": round(r.cycles / wall, 1),
            "cpu_s": r.cpu_s, "peak_rss_mb": r.peak_rss_mb, "final_equity": r.final_equity / MICROS,
            "net_return": round(r.final_equity / r.initial_cash - 1, 6), "orders": len(r.orders),
            "ledger_rejections": r.ledger_rejections, "fills": r.fills, "clob_trades": r.trades,
            "turnover_x_capital": round(r.turnover, 2), "fees": r.fees / MICROS, "slippage": r.slippage / MICROS,
            "mean_exposure_pct": round(r.exposure_bps_mean / 100, 1), "intent_outcomes": r.intent_outcomes,
            "reconciliation_breaks": len(r.reconciliation_breaks), "timings_us": r.timings, "gateway": r.gateway,
            "ledger_digest": r.ledger_digest, "clob_head_hash": r.clob_head_hash,
            "attribution": {k: {"pnl": v["pnl"] / MICROS, "fees": v["fees"] / MICROS, "fills": v["fills"]}
                            for k, v in r.attribution.items()},
            "example_aggregate_order": multi and multi["explanation"],
            "example_internal_netting": netted and netted["explanation"]}


def background_research() -> subprocess.Popen:
    """One lower-priority parameter search, the kind the research job runs (at most one at a time)."""
    code = ("import os; os.nice(10); os.environ['QS_TOURNAMENT_PROCS']='1'; os.environ['QS_LLM_PROVIDER']='offline';"
            "from hellabandz.agents import tournament; from pathlib import Path; import tempfile;"
            "os.environ['QS_MANIFESTS']=tempfile.mkdtemp();"
            "import hellabandz.ingestion.registry as r; r.MANIFESTS=Path(os.environ['QS_MANIFESTS']);"
            f"tournament.run(Path({str(FIXTURE)!r}), 'BGRESEARCH')")
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=0, help="limit cycles (0 = whole held-out period)")
    ap.add_argument("--json")
    ap.add_argument("--background-research", action="store_true")
    a = ap.parse_args()
    out = {"cpu_count": os.cpu_count(), "cgroup_cpu_max": _read("/sys/fs/cgroup/cpu.max"),
           "cgroup_memory_max": _read("/sys/fs/cgroup/memory.max"), "runs": []}
    for mode in (VenueMode.HISTORICAL_SIM, VenueMode.PAPER_CLOB):
        out["runs"].append(run(mode, a.cycles or None))
    if a.background_research:
        proc = background_research()
        time.sleep(1.0)
        out["with_background_research"] = run(VenueMode.PAPER_CLOB, a.cycles or None)
        out["background_research_still_running"] = proc.poll() is None
        proc.kill()
    for r in out["runs"] + ([out["with_background_research"]] if "with_background_research" in out else []):
        t = r["timings_us"]
        print(f"{r['mode']:15} cycles={r['cycles']} {r['cycles_per_s']}/s return={r['net_return']:+.2%} orders={r['orders']} "
              f"fills={r['fills']} breaks={r['reconciliation_breaks']} turnover={r['turnover_x_capital']}x "
              f"cycle p50/p99={t['cycle'].get('p50_us')}/{t['cycle'].get('p99_us')}us digest={r['ledger_digest'][:12]}")
        print(f"  {r['example_aggregate_order']}")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


if __name__ == "__main__":
    main()
