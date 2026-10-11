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
from concurrent.futures import ThreadPoolExecutor, wait

import httpx

from hellabandz.backtester import SnapshotProvider
from hellabandz.contracts import (MICROS, ApplyBar, ApplyVenueFill, CancelOrder, OpenPortfolio, SetAllocation,
                                  SubmitOrder, SubmitVenueOrder)
from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.ledger import PortfolioState, replay
from hellabandz.trading.coordinator import EXEC_AGENT, paper_budget
from hellabandz.trading.execution import VenueMode
from hellabandz.trading.instruments import instruments_for_dataset
from hellabandz.trading.registry import Registry, RegistryError
from hellabandz.trading.research import (INITIAL, data_class, portfolio_sleeves, research, research_current)
from hellabandz.trading.research import core_version, DEPLOYABLE, research_metrics
from hellabandz.trading.reporting import simulation_context, registry_path, save_report, live_report
from hellabandz.ingestion.registry import MANIFESTS
from hellabandz.trading.runtime import CoordinatedRuntime, percentiles
from hellabandz.trading.strategies import FAMILY_CARDS, RuntimeStrategy, StrategyVersion

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
            if len(rows) < 5000:
                return new

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

    def process_bars(self, bars) -> tuple[list[dict], dict]:
        """One HTTP round trip for a same-timestamp group with no resting orders."""
        resp = self._post("/v1/bars", {"dataset_id": f"coordinated:{self.run_id}",
            "bars": [json.loads(b.model_dump_json()) for b in bars]})
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
    progress("loading_market_data", 0)
    t, meta = load_snapshot(dataset_dir)
    p = SnapshotProvider(t)
    reg = Registry(registry_path(meta['dataset_id']))
    mode = config.get("allocation_mode", "approved")
    progress("checking_frozen_strategies", 0)
    context = simulation_context(reg, meta, mode)
    research_wait_deadline = time.monotonic() + float(config.get("research_budget_s", 600)) + 60
    while not context["current"] and not stop_requested():
        progress("research", 0)
        try:
            research(dataset_dir, reg, budget_s=float(config.get("research_budget_s", 600)),
                     use_llm=True,
                     progress=lambda stage, role, n: progress(f"research:{role}:{stage}",
                         n if stage == "optimized" and type(n) is int else None),
                     stop_requested=stop_requested)
        except RegistryError as exc:
            if "research already running" not in str(exc):
                raise
            if time.monotonic() >= research_wait_deadline:
                raise TimeoutError("timed out waiting for compatible research") from exc
            progress("waiting_for_research", 0)
            time.sleep(.5)
            reg = Registry(registry_path(meta['dataset_id']))
            context = simulation_context(reg, meta, mode)
            continue
        context = simulation_context(reg, meta, mode)
        break
    if stop_requested():
        return {"run_id": run_id, "portfolio_id": pid, "stopped": True, "cycles": 0,
                "detail": "Stopped before creating trading commands."}
    universe = sorted(meta["symbols"])
    # The report itself records every exploratory replay. Avoid a registry write
    # here: a simultaneous research writer must retain its own frozen state.
    context.update(holdout_views=reg.data.get("holdout_views", {}).get(meta["content_sha256"], 0),
                   holdout_count_recorded=False, holdout_untouched=False)
    if not context["current"]:
        raise ValueError("coordinated execution requires completed compatible research")
    if mode == "experimental":
        from hellabandz.trading.strategies import make_version
        sleeves = [(a["agent_id"], make_version(a["agent_id"], FAMILY_CARDS[a["family"]], a["params"], a["universe"]),
                    a["capital"]) for a in context["agents"] if a["funded"]]
    else:
        sleeves = portfolio_sleeves(reg, universe)
    funded = {s for s, _, _ in sleeves}
    # challengers that were not funded run in shadow: their intents are shown, never executed
    shadow = []
    for rec in context["agents"]:
        if not rec["funded"]:
            from hellabandz.trading.research import make_version
            card = FAMILY_CARDS[rec["family"]]
            v = make_version(rec["agent_id"], card, rec["params"], rec["universe"])
            shadow.append(RuntimeStrategy(v, "paper_clob"))
    progress("connecting_execution_worker", 0)
    ledger = ApiLedger(api, pid, headers, run_id)
    strats = [RuntimeStrategy(StrategyVersion(sid, v.family, v.params, v.universe, v.version, v.card), "paper_clob")
              for sid, v, _ in sleeves]
    rt = CoordinatedRuntime(run_id, VenueMode.PAPER_CLOB, strats, {sid: cap for sid, _, cap in sleeves},
                            paper_budget(INITIAL), instruments_for_dataset(meta), data_class(meta), INITIAL, ledger=ledger,
                            keep_decisions=False, keep_telemetry=True)
    # Synthetic LP quotes are ephemeral replay machinery. The CLOB still hashes
    # every event; financial fills remain in the durable ledger and full export.
    rt.authority.keep_clob_events = False
    if ledger.state.opened or ledger.state.seq:
        raise ValueError("coordinated simulation requires a fresh, unopened portfolio")
    for o in list(ledger.state.orders.values()):  # a previous process's paper book is gone: release its orders
        if o.agent_id == EXEC_AGENT and o.venue == "paper_clob":
            ledger.process(CancelOrder(client_order_id=o.order_id, agent_id=EXEC_AGENT))
    if not ledger.state.opened:
        progress("opening_paper_account", 0)
        rt.open()
    te0, te1 = meta["splits"]["test"]
    report = {"schema": "coordinated-simulation-1", "portfolio_id": pid, "run_id": run_id,
              "context": context, "config": config, "status": "starting", "points": [],
              "decisions": [], "rule_readings": [], "steps": [], "benchmark": {"status": "pending"}}
    save_report(MANIFESTS, pid, report)
    _say(ledger, run_id, "coordinator_start", {"mode": "paper_clob", "context": context,
         "sleeves": [(s, v.version, c) for s, v, c in sleeves], "shadow": [x.v.strategy_id for x in shadow]})
    progress("warming_strategy_history", 0)
    rt.warm(p, te0)
    baseline = CoordinatedRuntime(run_id + "-baseline", VenueMode.PAPER_CLOB,
        [RuntimeStrategy(core_version(universe), "paper_clob")], {"core": DEPLOYABLE},
        paper_budget(INITIAL), instruments_for_dataset(meta), data_class(meta), INITIAL)
    baseline.open()
    baseline.authority.keep_clob_events = False
    progress("warming_baseline_history", 0)
    baseline.warm(p, te0)
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
    if config.get("crash_at") is not None:
        raise ValueError("scripted crashes are unsupported in coordinated replay")
    if config.get("duration_s") and groups:
        speed = len(groups) / float(config["duration_s"])
    report["status"] = "running"
    save_report(MANIFESTS, pid, report)
    progress("ready_to_trade", 0, len(groups))
    previous = {}
    from hellabandz.agents import llm
    thinker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coordinated-commentary")
    futures = []
    def comment(agent, facts):
        try:
            raw, model = llm.chat_json("Comment on the supplied historical fixed-rule facts in one short sentence. "
                "Return JSON {\"thought\":string}. You cannot change rules, place orders, or claim a proven edge.",
                json.dumps(facts), timeout=10, max_retries=0)
            if str(raw.get("thought", "")).strip():
                _say(ledger, run_id, "thought", {"step": facts["step"], "ts": facts["ts"],
                     "thought": str(raw["thought"])[:400], "llm": model, "facts": facts,
                     "execution_effect": "none; commentary only"}, agent)
        except Exception as exc:
            _say(ledger, run_id, "thinking_unavailable", {"reason": type(exc).__name__,
                "execution_effect": "none; fixed rules continue"}, agent)
    if not llm.available():
        _say(ledger, run_id, "thinking_unavailable", {"reason": "no AI model configured"})
    t_start = time.perf_counter()
    for i, g in enumerate(groups):
        if stop_requested():
            break
        rec = rt.step(g)
        baseline.step(g)
        snap = rt.gateway.snapshot()
        marks = {sym: v[1] for sym, v in ledger.state.last_bar.items()}
        attribution = rt.book.report(marks)
        point_agents = {a["agent_id"]: {"pnl": attribution.get(a["agent_id"], {}).get("pnl", 0),
            "exposure": attribution.get(a["agent_id"], {}).get("market_value", 0)} for a in context["agents"]}
        report["points"].append({"seq": ledger.state.seq, "ts": snap.ts, "equity": rec.equity,
            "exposure": ledger.state.market_value(), "agents": point_agents})
        report["steps"].append({"step": i + 1, "ts": snap.ts, "equity": rec.equity,
            "cash": ledger.state.cash, "closes": marks, "sleeves": attribution,
            "unallocated_cash": rt.book.unallocated, "fees": ledger.state.fees_paid,
            "slippage": ledger.state.slippage_cost})
        shadow_intents = [x for s in shadow for x in s.on_snapshot(snap)]
        intents = rt.last_intents
        for agent in context["agents"]:
            aid = agent["agent_id"]
            readings = [x.model_dump(mode="json") for x in intents + shadow_intents if x.strategy_id == aid]
            symbols = {x["instrument"]: {"target_bps": x["target_weight_bps"],
                "previous_bps": previous.get((aid, x["instrument"])),
                "reading": "; ".join(x["reasons"]) + " " + json.dumps(x["features"], sort_keys=True),
                "features": x["features"], "score": x["score"], "weight_scope": x["weight_scope"]} for x in readings}
            signal = {"step": i + 1, "ts": snap.ts, "agent": aid, "funded": agent["funded"],
                      "opening": i == 0, "symbols": symbols, "intents": readings,
                      "execution_effect": "coordinator input" if agent["funded"] else "shadow only; no orders"}
            report["rule_readings"].append(signal)
            if i % 5 == 0 or i == len(groups) - 1 or any(x["target_bps"] != x["previous_bps"] for x in symbols.values()):
                _say(ledger, run_id, "signal", signal, aid)
            previous.update({(aid, sym): v["target_bps"] for sym, v in symbols.items()})
            if aid != "core" and i in (0, len(groups) // 2) and llm.available():
                futures.append(thinker.submit(comment, aid, {"step": i + 1, "ts": snap.ts,
                    "funded": agent["funded"], "family": agent["family"], "symbols": symbols,
                    "sleeve": attribution.get(aid, {}), "promotion_reasons": agent["reasons"]}))
        for d in rt.last_decisions:
            report["decisions"].append(d.model_dump(mode="json"))
            if d.order or d.transfers or d.cancel_order_id or any(r.outcome.value == "rejected" for r in d.intents):
                _say(ledger, run_id, "coordinator_decision", d.model_dump(mode="json"))
        if i % 5 == 0 or i == len(groups) - 1:
            _say(ledger, run_id, "coordinator_state", state_message(rt, reg, snap, intents, shadow_intents, ledger, meta,
                                                                    "paper_clob"))
        # This baseline advances only alongside completed actual cycles, including
        # a stopped/truncated run, using identical cash, fees, risk and paper venue.
        report["benchmark"] = {"status": "available", "initial_cash": INITIAL,
            "start_ts": te0, "end_ts": snap.ts + 1,
            "assumptions": "Same completed bars, initial cash, deployable budget, coordinator limits, fees and synthetic paper CLOB. Equal-weight buy and hold; no crash overlay.",
            "baseline": [{"ts": r.ts, "equity": r.equity} for r in baseline.records],
            "strategy": {"ts": [r.ts for r in rt.records], "equity": [r.equity for r in rt.records]}}
        if i % 5 == 0 or i == len(groups) - 1:
            save_report(MANIFESTS, pid, live_report(report), live=True)
        # The archive is for download/recovery, not live polling. Avoid serializing
        # the entire accumulated history to a host-mounted file every five cycles.
        if i % 100 == 0 or i == len(groups) - 1:
            save_report(MANIFESTS, pid, report)
        progress("coordinating", i + 1, len(groups))
        if speed > 0:
            delay = (i + 1) / speed - (time.perf_counter() - t_start)
            if delay > 0:
                time.sleep(min(delay, 5))
    v = ledger.c.get("/v1/verify").json()
    res = rt.result()
    bm = research_metrics(baseline.result())
    sm = research_metrics(res)
    report["benchmark"].update(baseline_metrics=bm, strategy_metrics=sm)
    summary = {"run_id": run_id, "portfolio_id": pid, "cycles": res.cycles, "final_equity": res.final_equity / MICROS,
               "net_return": res.final_equity / res.initial_cash - 1, "orders": len(res.orders), "fills": res.fills,
               "reconciliation_breaks": len(res.reconciliation_breaks), "verify": v,
               "sleeves": [(s, vv.version, c / MICROS) for s, vv, c in sleeves], "intent_outcomes": res.intent_outcomes,
               "elapsed_s": round(time.perf_counter() - t_start, 1),
               "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    summary.update(metrics=sm, baseline_metrics=bm, fees=res.fees, slippage=res.slippage,
                   turnover=res.turnover, exposure_bps_mean=res.exposure_bps_mean,
                   ledger_rejections=res.ledger_rejections, attribution=res.attribution,
                   unallocated_cash=rt.book.unallocated, ledger_digest=res.ledger_digest,
                   head_hash=res.head_hash, return_attribution=res.return_attribution,
                   policy_version=context["policy_version"], code_version=context["code_version"],
                   source_sha256=context["source_sha256"], dataset_sha256=meta["content_sha256"],
                   stopped=stop_requested(), actual_end_ts=res.equity_ts[-1] + 1 if res.equity_ts else te0)
    wait(futures, timeout=12)
    thinker.shutdown(wait=False, cancel_futures=True)
    report.update(status="stopped" if stop_requested() else "completed", summary=summary,
                  final_attribution=res.attribution, telemetry=res.telemetry)
    save_report(MANIFESTS, pid, report)
    save_report(MANIFESTS, pid, live_report(report), live=True)
    _say(ledger, run_id, "coordinator_summary", summary)
    return summary
