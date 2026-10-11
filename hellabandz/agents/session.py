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
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from hellabandz.agents import llm
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
        self.stop_requested = stop_requested
        self.progress = progress
        # the passive core (when the manifest has one) trades like any other slot but takes no crisis decisions
        entries = manifest["fitted"] + ([manifest["core"]] if manifest.get("core") else [])
        self.specs = {f["spec"]["agent_id"]: StrategySpec.model_validate(f["spec"]) for f in entries}
        self.alloc = {self.specs[a].agent_id: manifest["allocations"]["by_strategy"][s.strategy_id]["capital_micros"]
                      for a, s in self.specs.items()}
        self.base_alloc = dict(self.alloc)
        self.initial_cash = manifest.get("policy", {}).get("initial_cash_micros", 10_000 * MICROS)
        self.stats = {"bars": 0, "orders": 0, "approved": 0, "reduced": 0, "rejected": 0, "duplicates": 0}

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
        result = self.c.post(path, params={"wait": False}, **kwargs).json()
        seq = result.get("command_seq", result.get("last_command_seq"))
        deadline = time.monotonic() + 300
        while seq and (result.get("status") == "queued" or result.get("processed") is False):
            if self.stop_requested():
                raise SessionStopped()
            if time.monotonic() > deadline:
                raise TimeoutError("execution command still queued; restart the selected portfolio worker")
            self.progress("waiting_for_execution", None, None)
            time.sleep(0.2)
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

    def run(self, max_steps: int | None, speed: float, crash_at: float | None, retry_dupes: bool = True) -> dict:
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
        r = self.c.get("/v1/portfolio")
        seen = max((v[0] for v in r.json()["state"].get("last_bar", {}).values()), default=0) if r.status_code == 200 else 0
        if groups and seen >= groups[0][0].ts.timestamp():
            # bars are idempotent and ordered, so a second replay cannot move the ledger: every order would be stale (R7)
            raise ValueError(f"portfolio {self.pid!r} has already replayed this period; select a fresh portfolio")
        self.say("system", "session_start", {"steps": len(groups), "crash_start_step": crash_start,
                                             "units": self.units,
                                             "speed_steps_per_s": speed})
        base_hist = {sym: self.p.history(sym, te[0]) for sym in self.p.symbols()}
        live_ts: dict[str, list[int]] = {s: [] for s in self.p.symbols()}
        live_close: dict[str, list[float]] = {s: [] for s in self.p.symbols()}
        live_high: dict[str, list[float]] = {s: [] for s in self.p.symbols()}
        live_low: dict[str, list[float]] = {s: [] for s in self.p.symbols()}
        t_start = time.perf_counter()
        completed_steps = 0
        for i, g in enumerate(groups):
            if self.stop_requested():
                break
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
            r = self.post("/v1/bars",
                            json={"dataset_id": f"session:{self.run_id}",
                                  "bars": [json.loads(b.model_dump_json()) for b in pub]})
            self.stats["bars"] += len(pub)
            closes = {b.symbol: b.close for b in pub}
            for b in pub:
                live_ts[b.symbol].append(int(b.ts.timestamp()))
                live_close[b.symbol].append(b.close / MICROS)
                live_high[b.symbol].append(b.high / MICROS)
                live_low[b.symbol].append(b.low / MICROS)
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
                if self.alloc[agent] <= 0:
                    continue
                histories = {}
                for sym in spec.universe:
                    bh, bts = base_hist[sym]
                    histories[sym] = (Series(np.concatenate([bh.close, live_close[sym]]),
                                             np.concatenate([bh.high, live_high[sym]]),
                                             np.concatenate([bh.low, live_low[sym]])),
                                      np.concatenate([bts, np.array(live_ts[sym], dtype=np.int64)]))
                weights = universe_weights(spec.family, spec.params, histories)
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
                    if retry_dupes and i % 25 == 0:  # simulate a flaky client retrying: must be a no-op
                        self.submit(agent, sym, act[0], act[1], g[0].ts)
                    if res.get("outcome") in ("REJECT", "REDUCE"):
                        self.say(agent, "order_" + res["outcome"].lower(),
                                 {"symbol": sym, "side": act[0], "qty": act[1], "approved_qty": res.get("approved_qty"),
                                  "rules": res.get("rules")})
            if speed > 0:
                delay = max(0.0, (i + 1) / speed - (time.perf_counter() - t_start))
                while delay > 0 and not self.stop_requested():
                    time.sleep(min(delay, 0.2))
                    delay -= 0.2
            completed_steps = i + 1
            self.progress("replaying", completed_steps, len(groups))
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
