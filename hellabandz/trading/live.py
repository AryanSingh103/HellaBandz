"""Coordinated session through the real system: API -> Postgres command log -> execution worker -> ledger.

The coordinated runtime runs in the job process, but every ledger command goes over HTTP to the
portfolio's execution worker, exactly like the existing session client. ApiLedger keeps a local mirror
of the portfolio by replaying the worker's hash-chained events (verifying the chain), so the
coordinator decides on the same state the worker has, and nothing financial is computed twice.

What the browser sees is posted as agent messages for the portfolio:
  - coordinator_decision: every decision that sent, cancelled or netted something, or rejected an intent
  - coordinator_state: a periodic snapshot of mode, data freshness, intents, targets, exposure, working
    orders, fills by venue, strategy versions and promotion history, latency and resource metrics,
    synthetic depth (labeled), and the kill switch

Restart behavior: a coordinated job is not resumed after a manager restart (the job is marked
interrupted, like ordinary sessions). Its paper CLOB lives in the job process, so on any start the
runner cancels working paper_clob orders the ledger still holds for the execution owner before trading.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from hellabandz.backtester import SnapshotProvider
from hellabandz.contracts import (MICROS, ApplyBar, ApplyVenueFill, CancelOrder, OpenPortfolio, SetAllocation,
                                  SubmitOrder, SubmitVenueOrder)
from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.ledger import PortfolioState, replay
from hellabandz.trading.coordinator import EXEC_AGENT, paper_budget
from hellabandz.trading.execution import VenueMode
from hellabandz.trading.instruments import instruments_for_dataset
from hellabandz.trading.registry import Registry
from hellabandz.trading.research import (INITIAL, data_class, portfolio_sleeves, research)
from hellabandz.trading.runtime import CoordinatedRuntime, percentiles
from hellabandz.trading.strategies import RuntimeStrategy, StrategyVersion

ROOT = Path(__file__).resolve().parents[2]


class ApiLedger:
    def __init__(self, api: str, portfolio: str, headers: dict | None = None, run_id: str = "coord"):
        self.c = httpx.Client(base_url=api, timeout=60, params={"portfolio_id": portfolio}, headers=headers)
        self.pid, self.run_id = portfolio, run_id
        self.state = PortfolioState()
        self.events: list[dict] = []
        self.calls = 0
        self.latency_ns: list[int] = []
        self.sync()

    def sync(self) -> list[dict]:
        new = []
        while True:
            rows = self.c.get("/v1/events", params={"after_seq": self.state.seq, "limit": 5000}).raise_for_status().json()
            if not rows:
                return new
            evs = [r["event"] for r in rows]
            replay(evs, verify_chain=True, initial_state=self.state)
            self.events.extend(evs)
            new.extend(evs)

    def _post(self, path: str, body: dict, **kw) -> dict:
        t0 = time.perf_counter_ns()
        r = self.c.post(path, json=body, **kw)
        if r.status_code == 409:
            return r.json().get("detail", {"status": "conflict"})
        r.raise_for_status()
        out = r.json()
        deadline = time.monotonic() + 120
        seq = out.get("command_seq") or out.get("last_command_seq")
        while (out.get("status") == "queued" or out.get("processed") is False) and seq:
            if time.monotonic() > deadline:
                raise TimeoutError("execution worker did not process the command")
            time.sleep(0.05)
            cmd = self.c.get(f"/v1/commands/{seq}").raise_for_status().json()
            if cmd["status"] == "done":
                out = {**out, **(cmd.get("response") or {}), "processed": True}
        self.latency_ns.append(time.perf_counter_ns() - t0)
        self.calls += 1
        return out

    def process(self, cmd) -> tuple[list[dict], dict]:
        if isinstance(cmd, OpenPortfolio):
            resp = self._post("/v1/admin/portfolio", {"initial_cash_usd": f"{cmd.initial_cash / MICROS:.6f}"})
        elif isinstance(cmd, SetAllocation):
            resp = self._post("/v1/admin/allocations", {"agent_id": cmd.agent_id, "capital_usd": f"{cmd.capital / MICROS:.6f}"},
                              headers={"Idempotency-Key": f"{self.run_id}:{cmd.agent_id}:{cmd.capital}"})
        elif isinstance(cmd, ApplyBar):
            resp = self._post("/v1/bars", {"dataset_id": f"coordinated:{self.run_id}", "bars": [json.loads(cmd.bar.model_dump_json())]})
            resp = {"status": "applied"}
        elif isinstance(cmd, SubmitOrder):
            resp = self._post("/v1/orders", json.loads(cmd.order.model_dump_json()))
        elif isinstance(cmd, SubmitVenueOrder):
            resp = self._post("/v1/venue/orders", {"venue": cmd.venue, "order": json.loads(cmd.order.model_dump_json())})
        elif isinstance(cmd, ApplyVenueFill):
            resp = self._post("/v1/venue/fills", json.loads(cmd.model_dump_json()))
        elif isinstance(cmd, CancelOrder):
            resp = self._post(f"/v1/orders/{cmd.client_order_id}/cancel", None, params={"agent_id": cmd.agent_id,
                                                                                        "portfolio_id": self.pid})
        else:
            raise TypeError(f"unsupported command {type(cmd).__name__}")
        return self.sync(), resp


def _say(ledger: ApiLedger, run_id: str, role: str, content: dict, agent: str = "coordinator") -> None:
    try:
        ledger.c.post("/v1/agent-messages", json={"run_id": run_id, "agent_id": agent, "role": role,
                                                   "content": content, "portfolio_id": ledger.pid})
    except httpx.HTTPError:
        pass


def state_message(rt: CoordinatedRuntime, reg: Registry, snap, intents: list, shadow_intents: list, ledger: ApiLedger,
                  dataset: dict, mode: str) -> dict:
    s = rt.ledger.state
    marks = {sym: v[1] for sym, v in s.last_bar.items()}
    fills = [e for e in rt.ledger.events if e["type"] == "OrderFilled"]
    by_venue: dict[str, int] = {}
    for e in fills:
        k = f"{e.get('venue', 'bar_sim')}/{e.get('liquidity', 'bar_model')}"
        by_venue[k] = by_venue.get(k, 0) + 1
    clob = rt.authority.clob
    return {
        "mode": mode, "venue": rt.coordinator.venue, "data_class": rt.gateway.data_class.value,
        "data_source": dataset.get("source"), "dataset_id": dataset.get("dataset_id"),
        "cycle": rt.coordinator.state.cycle, "market_ts": snap.ts,
        "freshness": {sym: {"last_ts": v.last_ts, "age_s": snap.ts - v.last_ts, "needs_resync": v.needs_resync}
                      for sym, v in snap.instruments.items()},
        "kill_switch": s.kill_switch, "kill_reason": s.kill_reason,
        "account": {"equity": s.equity(), "cash": s.cash, "reserved_cash": s.reserved_cash, "market_value": s.market_value(),
                    "positions": {k.split("|", 1)[1]: {"qty": p.qty, "basis": p.basis, "mark": marks.get(k.split("|", 1)[1])}
                                  for k, p in s.positions.items() if p.qty and k.startswith(EXEC_AGENT + "|")},
                    "fees": s.fees_paid, "slippage": s.slippage_cost},
        "working_orders": [{"order_id": o.order_id, "symbol": o.symbol, "side": o.side, "remaining": o.remaining,
                            "limit_px": o.limit_px, "venue": o.venue} for o in s.orders.values()],
        "fills_by_venue": by_venue, "external_fills": 0,
        "intents": [i.model_dump() for i in intents], "shadow_intents": [i.model_dump() for i in shadow_intents],
        "sleeves": rt.book.report(marks), "unallocated_cash": rt.book.unallocated,
        "strategies": [{**st.v.describe(), "capital": rt.active[st.v.strategy_id].capital} for st in rt.strategies],
        "champions": reg.champions(), "registry_history": reg.data["history"][-20:],
        "timings": {**{k: percentiles(v[-500:]) for k, v in rt.timings.items() if k not in ("submit", "match")},
                    **{k: percentiles(v[-500:]) for k, v in rt.authority.report.timings_ns.items()}},
        "ledger_round_trip": percentiles(ledger.latency_ns[-500:]), "ledger_calls": ledger.calls,
        "gateway": vars(rt.gateway.stats).copy(),
        "depth": {sym: {**clob.depth(sym, 3), "label": "synthetic ladder from OHLCV bars (participant sim_lp), not exchange depth"}
                  for sym in sorted(clob.books)} if clob else {},
        "intent_outcomes": rt.outcomes,
    }


def run_coordinated(job: dict, dataset_dir: Path, api: str, headers: dict, progress, stop_requested) -> dict:
    config = job["config"]
    pid, run_id = job["portfolio_id"], job["run_id"]
    t, meta = load_snapshot(dataset_dir)
    p = SnapshotProvider(t)
    reg = Registry(ROOT / "data/registry" / f"{meta['dataset_id']}.json")
    if not reg.data["versions"]:
        progress("research")
        research(dataset_dir, reg, budget_s=float(config.get("research_budget_s", 600)),
                 progress=lambda stage, role, n: progress(f"research:{role}:{stage}"))
    universe = sorted(meta["symbols"])
    sleeves = portfolio_sleeves(reg, universe)
    funded = {s for s, _, _ in sleeves}
    # challengers that were not funded run in shadow: their intents are shown, never executed
    shadow = []
    for key, rec in sorted(reg.data["versions"].items()):
        if rec["role"] not in funded and rec.get("status") == "rejected" and rec["universe"] == universe:
            from hellabandz.trading.research import make_version
            card = {"sma_crossover": "trend", "zscore_reversion": "mean_reversion", "vol_target": "defensive",
                    "regime_reversion": "regime"}[rec["family"]]
            v = make_version(rec["role"], card, rec["params"], universe)
            if not any(x.v.strategy_id == v.strategy_id for x in shadow):
                shadow.append(RuntimeStrategy(v, "paper_clob"))
    ledger = ApiLedger(api, pid, headers, run_id)
    strats = [RuntimeStrategy(StrategyVersion(sid, v.family, v.params, v.universe, v.version, v.card), "paper_clob")
              for sid, v, _ in sleeves]
    rt = CoordinatedRuntime(run_id, VenueMode.PAPER_CLOB, strats, {sid: cap for sid, _, cap in sleeves},
                            paper_budget(INITIAL), instruments_for_dataset(meta), data_class(meta), INITIAL, ledger=ledger)
    for o in list(ledger.state.orders.values()):  # a previous process's paper book is gone: release its orders
        if o.agent_id == EXEC_AGENT and o.venue == "paper_clob":
            ledger.process(CancelOrder(client_order_id=o.order_id, agent_id=EXEC_AGENT))
    if not ledger.state.opened:
        rt.open()
    te0, te1 = meta["splits"]["test"]
    rt.warm(p, te0)
    for sh in shadow:
        sh.warm_universe({s: (ts, h.close, h.high, h.low) for s, (h, ts) in ((s, p.history(s, te0)) for s in universe)})
    groups = []
    for b in p.bars(te0, te1):
        if groups and groups[-1][0].ts == b.ts:
            groups[-1].append(b)
        else:
            groups.append([b])
    steps = int(config.get("steps") or len(groups))
    speed = float(config.get("speed") or 0)
    groups = groups[:steps]
    _say(ledger, run_id, "coordinator_start", {"mode": "paper_clob", "sleeves": [(s, v.version, c) for s, v, c in sleeves],
                                               "shadow": [x.v.strategy_id for x in shadow], "steps": len(groups)})
    t_start = time.perf_counter()
    for i, g in enumerate(groups):
        if stop_requested():
            break
        rec = rt.step(g)
        snap = rt.gateway.snapshot()
        for d in rec.decisions:
            if d.order or d.transfers or d.cancel_order_id or any(r.outcome.value == "rejected" for r in d.intents):
                _say(ledger, run_id, "coordinator_decision", d.model_dump(mode="json"))
        if i % 5 == 0 or i == len(groups) - 1:
            intents = [x for s in rt.strategies for x in s.on_snapshot(snap)]
            shadow_intents = [x for s in shadow for x in s.on_snapshot(snap)]
            _say(ledger, run_id, "coordinator_state", state_message(rt, reg, snap, intents, shadow_intents, ledger, meta,
                                                                    "paper_clob"))
        progress("coordinating", i + 1, len(groups))
        if speed > 0:
            delay = (i + 1) / speed - (time.perf_counter() - t_start)
            if delay > 0:
                time.sleep(min(delay, 5))
    v = ledger.c.get("/v1/verify").json()
    res = rt.result()
    summary = {"run_id": run_id, "portfolio_id": pid, "cycles": res.cycles, "final_equity": res.final_equity / MICROS,
               "net_return": res.final_equity / res.initial_cash - 1, "orders": len(res.orders), "fills": res.fills,
               "reconciliation_breaks": len(res.reconciliation_breaks), "verify": v,
               "sleeves": [(s, vv.version, c / MICROS) for s, vv, c in sleeves], "intent_outcomes": res.intent_outcomes,
               "elapsed_s": round(time.perf_counter() - t_start, 1),
               "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _say(ledger, run_id, "coordinator_summary", summary)
    return summary
