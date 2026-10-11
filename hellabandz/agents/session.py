"""Live paper-trading session: replays the held-out period through the API.

This process is a *client* of the trading system. It publishes bars and
strategy orders over HTTP exactly like an external venue feed and trading
agents would; the execution worker decides what happens. A scripted crash
overlay (recorded in the session manifest) stresses the risk engine, and each
agent is asked (LLM or fallback) how to respond - its answer becomes ordinary
orders that go through the same risk checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from hellabandz.agents import llm
from hellabandz.agents.thinking import HOW, THINK_SYSTEM, reading
from hellabandz.backtester import SnapshotProvider, rebalance_order, reentry_capital
from pydantic import TypeAdapter
from hellabandz.contracts import MICROS, Bar, Ident, StrategyFamily, StrategySpec
from hellabandz.ingestion.dataset import load_snapshot, unit_context
from hellabandz.ledger.state import Position
from hellabandz.strategies import HOLD_FAMILIES, Series, universe_weights

log = logging.getLogger("session")
ROOT = Path(__file__).resolve().parents[2]


class SessionStopped(Exception):
    """Stop future production while preserving already queued commands."""


def _throttled_stop(check, every: float = 0.25):
    """The stop check reads the database; ask at most every `every` seconds. A stop, once seen, stays seen."""
    asked, stopped = [-1e9], [False]

    def stop_requested() -> bool:
        now = time.monotonic()
        if not stopped[0] and now - asked[0] >= every:
            asked[0], stopped[0] = now, bool(check())
        return stopped[0]
    return stop_requested


def _throttled_progress(report, every: float = 0.25):
    """Progress is a database write; report stage changes and the final step at once, the rest at most every `every` s."""
    last = {"stage": None, "at": -1e9}

    def progress(stage, value=None, total=None) -> None:
        now = time.monotonic()
        if stage != last["stage"] or now - last["at"] >= every or (value is not None and value == total):
            last["stage"], last["at"] = stage, now
            report(stage, value, total)
    return progress


class ApiState:
    """Read-only view of the worker's projection, shaped like PortfolioState for rebalance_order."""

    def __init__(self, st: dict):
        self.raw = st
        self.orders = {k: _Obj(v) for k, v in st.get("orders", {}).items()}
        self.kill_switch = bool(st.get("kill_switch"))
        self._pos = st.get("positions", {})

    def position(self, agent: str, sym: str) -> Position:
        p = self._pos.get(f"{agent}|{sym}")
        return Position(**p) if p else Position()


class _Obj:
    def __init__(self, d: dict):
        self.__dict__.update(d)


def crash_factor(i: int, start: int, depth: float = 0.16, fall_bars: int = 4, recover_bars: int = 120) -> float:
    """Multiplicative price overlay: sharp fall over `fall_bars`, slow linear recovery."""
    if i < start:
        return 1.0
    k = i - start
    if k < fall_bars:
        return 1.0 - depth * (k + 1) / fall_bars
    k -= fall_bars
    return 1.0 - depth * max(0.0, 1.0 - k / recover_bars)


SYSTEM_CRISIS = """You are the {agent} trading agent. The market just gapped down sharply (stress regime:
volumes tripled, slippage widened). Your current holdings and allocation are given. Choose ONE action:
"buy_dip" (add exposure), "reduce" (sell part of your holdings), or "hold". An independent deterministic
risk engine will approve, reduce, or reject whatever orders result - you cannot override it.
Respond with JSON: {{"action": "buy_dip"|"reduce"|"hold", "fraction": number between 0 and 1, "rationale": str}}"""

FALLBACK_CRISIS = {
    "momentum": {"action": "buy_dip", "fraction": 1.0,
                 "rationale": "Scripted aggressive response: triple exposure to buy the dip (expected to hit risk limits)."},
    "volatility": {"action": "reduce", "fraction": 0.5,
                   "rationale": "Realized volatility spiked far above target; cut exposure in half."},
    "mean_reversion": {"action": "hold", "fraction": 0.0, "rationale": "No allocation; stay flat."},
}


def decide_crisis(agent: str, held: dict[str, int], allocation_quote: float, prices: dict[str, float],
                  units: dict | None = None) -> dict:
    """LLM crisis decision, validated; deterministic fallback on any failure."""
    if llm.available():
        try:
            raw, meta = llm.chat_json(SYSTEM_CRISIS.format(agent=agent), json.dumps(
                {"holdings_execution_lots": held, "allocation_quote": allocation_quote,
                 "prices_quote_per_execution_lot": prices, "units": units or unit_context({})}))
            action = raw["action"]
            if action not in ("buy_dip", "reduce", "hold"):
                raise ValueError(f"unknown action {action!r}")
            return {"action": action, "fraction": min(max(float(raw.get("fraction", 0)), 0.0), 1.0),
                    "rationale": str(raw.get("rationale", ""))[:500], "source": "llm", "llm": meta}
        except Exception as e:
            return {**FALLBACK_CRISIS.get(agent, FALLBACK_CRISIS["mean_reversion"]),
                    "source": f"fallback ({type(e).__name__})"}
    return {**FALLBACK_CRISIS.get(agent, FALLBACK_CRISIS["mean_reversion"]), "source": "fallback"}


class Session:
    def __init__(self, api: str, manifest: dict, dataset_dir: Path, portfolio: str = "main",
                 stop_requested=lambda: False, progress=lambda *args: None, api_headers: dict | None = None):
        TypeAdapter(Ident).validate_python(portfolio)
        from hellabandz.api.client import api_headers as operator_headers
        headers = api_headers if api_headers is not None else operator_headers()
        self.c = httpx.Client(base_url=api, timeout=60, params={"portfolio_id": portfolio}, headers=headers,
                             event_hooks={"response": [lambda r: r.raise_for_status()]})
        self.m = manifest
        self.run_id = manifest["run_id"]
        self.pid = portfolio
        table, dataset = load_snapshot(dataset_dir)
        if manifest["dataset"]["content_sha256"] != dataset["content_sha256"]:
            raise ValueError("manifest and snapshot hashes do not match")
        self.p = SnapshotProvider(table)
        self.units = unit_context(dataset)
        self.price_tick = 1 if dataset.get("unit_version") == "base-lot-v2" else 10000
        self.stop_requested = _throttled_stop(stop_requested)
        self.progress = _throttled_progress(progress)
        self.report_progress = progress
        # the passive core (when the manifest has one) trades like any other slot but takes no crisis decisions
        entries = manifest["fitted"] + ([manifest["core"]] if manifest.get("core") else [])
        self.specs = {f["spec"]["agent_id"]: StrategySpec.model_validate(f["spec"]) for f in entries}
        self.alloc = {self.specs[a].agent_id: manifest["allocations"]["by_strategy"][s.strategy_id]["capital_micros"]
                      for a, s in self.specs.items()}
        self.base_alloc = dict(self.alloc)
        self.initial_cash = manifest.get("policy", {}).get("initial_cash_micros", 10_000 * MICROS)
        self.stats = {"bars": 0, "orders": 0, "approved": 0, "reduced": 0, "rejected": 0, "duplicates": 0}
        # display-only thinking records (see agents/thinking.py): last recorded rule target per (agent, symbol),
        # each agent's latest order outcomes, and AI commentary running off the replay thread
        self.targets: dict[tuple[str, str], int] = {}
        self.recent: dict[str, deque] = {a: deque(maxlen=5) for a in self.specs}
        self.thinkers = sorted(a for a, s in self.specs.items() if s.family != StrategyFamily.BUY_HOLD)
        self.thinker = ThreadPoolExecutor(max_workers=4, thread_name_prefix="think")
        self.poster = ThreadPoolExecutor(max_workers=1, thread_name_prefix="signal")  # one thread keeps readings in order
        self.thoughts: dict[str, Future] = {}
        self.thinking_reported = False

    def say(self, agent: str, role: str, content: dict) -> None:
        log.info("[%s] %s %s", agent, role, json.dumps(content, default=str)[:200])
        try:
            self.c.post("/v1/agent-messages", json={"run_id": self.run_id, "agent_id": agent, "role": role,
                                                     "content": content, "portfolio_id": self.pid})
        except httpx.HTTPError:
            pass

    def post(self, path: str, **kwargs) -> dict:
        if self.stop_requested():
            raise SessionStopped()
        # The API holds the request until the worker has processed the command and gives up after a few seconds
        # (still queued). Only then does the polling below take over, so a down worker never blocks a stop for long.
        result = self.c.post(path, params={"wait": True}, **kwargs).json()
        seq = result.get("command_seq", result.get("last_command_seq"))
        started, pause, first = time.monotonic(), 0.004, True
        while seq and (result.get("status") == "queued" or result.get("processed") is False):
            if self.stop_requested():
                raise SessionStopped()
            waited = time.monotonic() - started
            if waited > 300:
                raise TimeoutError("execution command still queued; restart the selected portfolio worker")
            if waited > 0.5:
                self.progress("waiting_for_execution", None, None)
            # the worker normally answers in about 10 ms, so one well-timed poll usually suffices; back off only
            # while it is busy or down
            time.sleep(0.008 if first else pause)
            if not first:
                pause = min(pause * 2, 0.2)
            first = False
            command = self.c.get(f"/v1/commands/{seq}").json()
            if command["status"] == "done":
                return {**result, **command["response"], "processed": True}
        return result

    def setup(self) -> None:
        r = self.post("/v1/admin/portfolio", json={"initial_cash_usd": "10000"})
        log.info("open portfolio: %s", r.get("status"))
        for agent, cap in sorted(self.alloc.items()):
            r = self.post("/v1/admin/allocations",
                            json={"agent_id": agent, "capital_usd": f"{cap / MICROS:.6f}"},
                            headers={"Idempotency-Key": f"{self.run_id}:{agent}"})
            log.info("allocation %s = %.2f %s: %s", agent, cap / MICROS, self.units["quote_currency"], r.get("status"))

    def submit(self, agent: str, sym: str, side: str, qty: int, ts: datetime, tag: str = "") -> dict:
        body = {"client_order_id": f"{self.run_id}.{agent}.{sym}.{int(ts.timestamp())}{tag}", "agent_id": agent,
                "symbol": sym, "side": side, "qty": qty, "signal_bar_ts": ts.isoformat()}
        r = self.post("/v1/orders", json=body)
        self.stats["orders"] += 1
        self.stats["duplicates"] += bool(r.get("duplicate_request"))
        key = {"APPROVE": "approved", "REDUCE": "reduced", "REJECT": "rejected"}.get(r.get("outcome"), "rejected")
        self.stats[key] += 1
        return r

    def observe(self, agent: str, spec: StrategySpec, histories: dict, weights: dict, closes: dict[str, int],
                step: int, ts: datetime, funded: bool) -> None:
        """Record the rule's reading when its target for a symbol flips or moves materially. Display only."""
        opening = not any(k[0] == agent for k in self.targets)
        changes = {}
        for sym in sorted(spec.universe):
            if sym not in closes:
                continue
            w, prev = int(weights[sym][-1]), self.targets.get((agent, sym))
            if prev is None or (w == 0) != (prev == 0) or abs(w - prev) >= 1500:
                self.targets[(agent, sym)] = w
                changes[sym] = {"target_bps": w, "previous_bps": prev, "price": closes[sym] / MICROS,
                                "reading": reading(spec.family.value, spec.params, histories[sym][0])}
        if changes:
            self.poster.submit(self.say, agent, "signal", {"step": step + 1, "ts": int(ts.timestamp()), "family": spec.family.value,
                                       "funded": funded, "opening": opening, "symbols": changes})

    def maybe_think(self, agent: str, spec: StrategySpec, histories: dict, weights: dict, closes: dict[str, int],
                    state: ApiState, step: int, total: int, ts: datetime, funded: bool, stress: bool) -> None:
        """Ask the agent's LLM for a short remark a few times per run, off the replay thread. It cannot trade."""
        if not llm.available():
            return
        every = max(1, total // 12)
        if step % every != (self.thinkers.index(agent) * every // len(self.thinkers)) % every:
            return
        running = self.thoughts.get(agent)
        if running and not running.done():
            return
        symbols = {}
        for sym in sorted(spec.universe):
            if sym not in closes:
                continue
            pos, c = state.position(agent, sym), histories[sym][0].close
            symbols[sym] = {"price": closes[sym] / MICROS, "rule_target_pct": int(weights[sym][-1]) / 100,
                            "held": pos.qty, "unrealized_pnl_quote": round((pos.qty * closes[sym] - pos.basis) / MICROS, 2),
                            "change_over_24_bars_pct": round((float(c[-1]) / float(c[-25]) - 1) * 100, 2) if len(c) > 25 else None,
                            "reading": reading(spec.family.value, spec.params, histories[sym][0])}
        facts = {"market_time": ts.isoformat(), "step": step + 1, "of_steps": total, "funded": funded,
                 "budget_quote": round(self.alloc[agent] / MICROS, 2), "crash_stress_now": stress,
                 "rule_settings": spec.params, "symbols": symbols, "recent_orders": list(self.recent[agent]),
                 "quote_currency": self.units["quote_currency"]}
        self.thoughts[agent] = self.thinker.submit(self.think, agent, spec.family.value, facts, int(ts.timestamp()))

    def think(self, agent: str, family: str, facts: dict, ts: int) -> None:
        try:
            raw, meta = llm.chat_json(THINK_SYSTEM.format(agent=agent, family=family, how=HOW.get(family, "a fixed rule")),
                                      json.dumps(facts, default=str), temperature=0.5, timeout=25, max_retries=0)
            thought = str(raw.get("thought", "")).strip()[:400]
            if thought:
                stance = raw.get("stance") if raw.get("stance") in ("adding", "holding", "trimming", "waiting") else None
                self.say(agent, "thought", {"step": facts["step"], "ts": ts, "thought": thought, "stance": stance,
                                            "source": "llm", "llm": meta, "facts": facts})
        except Exception as e:  # commentary is optional; the replay never depends on it
            log.warning("thought unavailable for %s: %s", agent, type(e).__name__)
            if not self.thinking_reported:
                self.thinking_reported = True
                self.say("system", "thinking_unavailable", {"step": facts["step"], "ts": ts, "reason": type(e).__name__})

    def crisis(self, ts: datetime, closes: dict[str, int], state: ApiState) -> None:
        for agent in sorted(self.specs):
            spec = self.specs[agent]
            if spec.family == StrategyFamily.BUY_HOLD:
                continue
            held = {s: state.position(agent, s).qty for s in spec.universe}
            act = decide_crisis(agent, held, self.alloc[agent] / MICROS,
                                {s: closes.get(s, 0) / MICROS for s in spec.universe}, self.units)
            frac = min(max(act["fraction"], 0.0), 1.0)
            results = []
            for sym in spec.universe:
                if sym not in closes:
                    continue
                if act["action"] == "buy_dip" and frac > 0 and self.alloc[agent] > 0:
                    qty = int(3 * frac * self.alloc[agent] / len(spec.universe) // closes[sym])
                    if qty > 0:
                        results.append({"symbol": sym, "side": "BUY", "qty": qty,
                                        **self.submit(agent, sym, "BUY", qty, ts, ".crisis")})
                elif act["action"] == "reduce" and held.get(sym, 0) > 0:
                    qty = max(1, int(held[sym] * frac))
                    results.append({"symbol": sym, "side": "SELL", "qty": qty,
                                    **self.submit(agent, sym, "SELL", qty, ts, ".crisis")})
            self.say(agent, "crisis_response", {"decision": act, "units": self.units, "orders": [
                {k: r.get(k) for k in ("symbol", "side", "qty", "status", "outcome", "approved_qty", "rules")}
                for r in results]})

    def run(self, max_steps: int | None, speed: float, crash_at: float | None, retry_dupes: bool = True,
            duration_s: float | None = None) -> dict:
        te = self.m["dataset"]["splits"]["test"]
        bars = list(self.p.bars(te[0], te[1]))
        groups: list[list[Bar]] = []
        for b in bars:
            if groups and groups[-1][0].ts == b.ts:
                groups[-1].append(b)
            else:
                groups.append([b])
        if max_steps:
            groups = groups[:max_steps]
        crash_start = int(len(groups) * crash_at) if crash_at is not None else None
        if duration_s and groups:
            speed = len(groups) / duration_s  # pace the whole replay to a wall-clock target; it can only slow it down
        r = self.c.get("/v1/portfolio")
        seen = max((v[0] for v in r.json()["state"].get("last_bar", {}).values()), default=0) if r.status_code == 200 else 0
        if groups and seen >= groups[0][0].ts.timestamp():
            # bars are idempotent and ordered, so a second replay cannot move the ledger: every order would be stale (R7)
            raise ValueError(f"portfolio {self.pid!r} has already replayed this period; select a fresh portfolio")
        self.say("system", "session_start", {"steps": len(groups), "crash_start_step": crash_start,
                                             "units": self.units,
                                             "speed_steps_per_s": speed, "target_duration_s": duration_s})
        if not llm.available():
            self.thinking_reported = True
            self.say("system", "thinking_unavailable", {"step": 0, "ts": 0, "reason": "no AI model configured"})
        base_hist = {sym: self.p.history(sym, te[0]) for sym in self.p.symbols()}
        # The published path (source bars with the scripted crash overlay) is fixed before the replay starts, and
        # every rule is causal: its target at a bar never depends on later bars. So each rule is evaluated once
        # over the whole path and read back bar by bar, instead of being recomputed over its full history at
        # every step. tests/unit/test_session_plan.py pins that the two give identical targets.
        published: list[list[Bar]] = []
        for i, g in enumerate(groups):
            f = crash_factor(i, crash_start) if crash_start is not None else 1.0
            stress = crash_start is not None and crash_start <= i < crash_start + 4
            pub = []
            for b in g:
                from decimal import Decimal, ROUND_HALF_EVEN
                tick = self.price_tick
                o, h, lo, c = (int((Decimal(x) * Decimal(str(f)) / tick).to_integral_value(rounding=ROUND_HALF_EVEN)) * tick
                               for x in (b.open, b.high, b.low, b.close))
                pub.append(Bar(symbol=b.symbol, ts=b.ts, open=o, high=max(h, o, c), low=min(lo, o, c), close=c,
                               volume=b.volume * (3 if stress else 1), stress=stress))
            published.append(pub)
        path: dict[str, tuple[Series, np.ndarray]] = {}
        for sym in self.p.symbols():
            bh, bts = base_hist[sym]
            live = [b for pub in published for b in pub if b.symbol == sym]
            path[sym] = (Series(np.concatenate([bh.close, [b.close / MICROS for b in live]]),
                                np.concatenate([bh.high, [b.high / MICROS for b in live]]),
                                np.concatenate([bh.low, [b.low / MICROS for b in live]])),
                         np.concatenate([bts, np.array([int(b.ts.timestamp()) for b in live], dtype=np.int64)]))
        plan = {agent: universe_weights(spec.family, spec.params, {sym: path[sym] for sym in spec.universe})
                for agent, spec in self.specs.items()}
        seen_bars = {sym: len(base_hist[sym][1]) for sym in self.p.symbols()}  # bars of each symbol published so far
        t_start = time.perf_counter()
        completed_steps = 0
        for i, g in enumerate(groups):
            if self.stop_requested():
                break
            stress = crash_start is not None and crash_start <= i < crash_start + 4
            pub = published[i]
            r = self.post("/v1/bars",
                            json={"dataset_id": f"session:{self.run_id}",
                                  "bars": [json.loads(b.model_dump_json()) for b in pub]})
            self.stats["bars"] += len(pub)
            closes = {b.symbol: b.close for b in pub}
            for b in pub:
                seen_bars[b.symbol] += 1
            port = self.c.get("/v1/portfolio").json()
            st, equity = ApiState(port["state"]), port["equity"]
            if crash_start is not None and i == crash_start:
                self.say("system", "market_crash", {"step": i, "ts": pub[0].ts.isoformat(),
                                                     "overlay": "-16% over 4 bars, 3x volume, stress slippage"})
                self.crisis(pub[0].ts, closes, st)
                port = self.c.get("/v1/portfolio").json()
                st, equity = ApiState(port["state"]), port["equity"]
            for agent in sorted(self.specs):
                spec = self.specs[agent]
                funded = self.alloc[agent] > 0
                if not funded and spec.family == StrategyFamily.BUY_HOLD:
                    continue
                # views of the path up to and including this step; nothing past it is visible below
                histories, weights = {}, {}
                for sym in spec.universe:
                    n = seen_bars[sym]
                    series, sym_ts = path[sym]
                    histories[sym] = (Series(series.close[:n], series.high[:n], series.low[:n]), sym_ts[:n])
                    weights[sym] = plan[agent][sym][:n]
                if spec.family != StrategyFamily.BUY_HOLD:
                    # unfunded agents trade nothing, but what their rule would do is still shown
                    self.observe(agent, spec, histories, weights, closes, i, g[0].ts, funded)
                    if not funded:
                        self.maybe_think(agent, spec, histories, weights, closes, st, i, len(groups), g[0].ts, funded, stress)
                if not funded:
                    continue
                if any(int(weights[sym][-1]) for sym in spec.universe if sym in closes):
                    resized = reentry_capital(st, spec, self.base_alloc[agent], self.initial_cash, equity)
                    if resized is not None and resized != self.alloc[agent]:
                        r = self.post("/v1/admin/allocations",
                                      json={"agent_id": agent, "capital_usd": f"{resized / MICROS:.6f}"},
                                      headers={"Idempotency-Key": f"{self.run_id}:{agent}:{int(g[0].ts.timestamp())}"})
                        if r.get("status") == "ok":
                            self.alloc[agent] = resized
                for sym in sorted(spec.universe):
                    if sym not in closes:
                        continue
                    w = int(weights[sym][-1])
                    act = rebalance_order(st, agent, sym, w, self.alloc[agent], closes[sym], len(spec.universe),
                                          hold=spec.family in HOLD_FAMILIES)
                    if act is None:
                        continue
                    res = self.submit(agent, sym, act[0], act[1], g[0].ts)
                    self.recent[agent].append({"symbol": sym, "side": act[0], "qty": act[1], "outcome": res.get("outcome"),
                                               "approved_qty": res.get("approved_qty"), "rules": res.get("rules")})
                    if retry_dupes and i % 25 == 0:  # simulate a flaky client retrying: must be a no-op
                        self.submit(agent, sym, act[0], act[1], g[0].ts)
                    if res.get("outcome") in ("REJECT", "REDUCE"):
                        self.say(agent, "order_" + res["outcome"].lower(),
                                 {"symbol": sym, "side": act[0], "qty": act[1], "approved_qty": res.get("approved_qty"),
                                  "rules": res.get("rules")})
                if spec.family != StrategyFamily.BUY_HOLD:
                    self.maybe_think(agent, spec, histories, weights, closes, st, i, len(groups), g[0].ts, funded, stress)
            if speed > 0:
                delay = max(0.0, (i + 1) / speed - (time.perf_counter() - t_start))
                while delay > 0 and not self.stop_requested():
                    time.sleep(min(delay, 0.2))
                    delay -= 0.2
            completed_steps = i + 1
            self.progress("replaying", completed_steps, len(groups))
        self.report_progress("replaying", completed_steps, len(groups))
        self.poster.shutdown(wait=True)
        # let commentary that is already in flight land before the summary; never wait long for it
        wait_futures([f for f in self.thoughts.values()], timeout=10)
        self.thinker.shutdown(wait=False, cancel_futures=True)
        v = self.c.get("/v1/verify").json()
        port = self.c.get("/v1/portfolio").json()
        summary = {"run_id": self.run_id, "portfolio_id": self.pid, "steps": completed_steps,
                   "stopped": self.stop_requested(), "crash_start_step": crash_start, **self.stats,
                   "final_equity_usd": port["equity"] / MICROS, "final_equity_quote": port["equity"] / MICROS,
                   "units": self.units, "legacy_usd_fields": "quote amounts; use units.quote_currency",
                   "ledger_digest": port["digest"],
                   "head_hash": port["head_hash"], "verify": v,
                   "elapsed_s": round(time.perf_counter() - t_start, 2)}
        self.say("system", "session_summary", summary)
        return summary


def latest_manifest() -> dict:
    files = sorted((f for f in (ROOT / "data/manifests").glob("T*.json") if f.suffixes == [".json"]),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("no tournament manifest found; run `make tournament` first")
    return json.loads(files[-1].read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("QS_API", "http://localhost:8000"))
    ap.add_argument("--manifest")
    ap.add_argument("--dataset", default=str(ROOT / "data/fixtures/synthetic_1h"))
    ap.add_argument("--portfolio", default="main")
    ap.add_argument("--steps", type=int, default=350, help="number of hourly timestamps to replay (0 = all)")
    ap.add_argument("--speed", type=float, default=4.0, help="timestamps per second (0 = as fast as possible)")
    ap.add_argument("--crash-at", type=float, default=0.5, help="fraction of the session where the crash starts")
    ap.add_argument("--no-crash", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    m = json.loads(Path(a.manifest).read_text()) if a.manifest else latest_manifest()
    s = Session(a.api, m, Path(a.dataset), a.portfolio)
    s.setup()
    out = s.run(a.steps or None, a.speed, None if a.no_crash else a.crash_at)
    from hellabandz.ingestion.registry import MANIFESTS
    path = MANIFESTS / f"{m['run_id']}.{a.portfolio}.session.json"
    path.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: v for k, v in out.items() if k != "verify"}, indent=1, default=str))
    print("verify:", out["verify"]["match"], out["verify"]["replayed_digest"])


if __name__ == "__main__":
    main()
