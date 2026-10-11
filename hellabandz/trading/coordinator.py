"""Portfolio coordinator: turns every strategy's intents into at most one order per instrument.

Decision procedure, run once per market snapshot, instruments in alphabetical order:

1. Portfolio gates. Kill switch on, or the day's loss limit hit: no new buying (exits still allowed).
2. Validate each intent: known strategy and active version, instrument in that strategy's universe,
   supported asset class, matching venue, not expired, not older than the last accepted intent from
   the same strategy for that instrument (out of order), not a repeat, and based on the current
   snapshot. Invalid intents are REJECTED with a reason code.
3. Data freshness. If the instrument's data is stale or needs resync, nothing is traded in it.
4. Desired lots per strategy = its budget for the instrument x target weight / price, rounded down.
   A change smaller than the rebalance band (default 10% of that budget) is ignored unless it is a
   full exit. This is the same hysteresis the research backtests use, so the live system does not
   churn more than the evaluated one.
5. Internal netting. Strategies that want fewer lots than they hold give lots to strategies that want
   more, at the last close, before anything goes to market (strategies sorted by id on both sides).
   Those intents are NETTED. This is the arbitration rule for conflicting signals: each strategy's
   target is honored inside its own budget; opposing wishes cancel out internally; only the net
   difference trades. Scores are never compared across strategies.
6. Order lifecycle. With a working order or an unacknowledged submission for the instrument, no new
   order is sent. If the working order still points the right way at about the right size it is
   kept; otherwise it is cancelled, and the replacement waits for the next cycle.
7. Limits on the net buy: per-order notional, committed capital, per-instrument and correlated-group
   concentration, gross exposure, cash after all reservations and after earlier orders in this same
   cycle, and daily turnover. Each limit that binds is recorded. Sells are capped by what the account
   holds (long only). Cooldown and minimum size defer small or too-frequent trades.
8. The order: one limit order at last close +/- the tightest slippage any contributing strategy
   accepts, with an attribution plan saying which strategies' lots it carries.

Everything here is deterministic: same snapshot, intents, account and configuration in, same decisions out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from hellabandz.ledger.engine import BPS
from hellabandz.trading.attribution import AttributionBook, split
from hellabandz.trading.instruments import COORDINATED_ASSET_CLASSES, Instrument
from hellabandz.trading.intents import Decision, Intent, IntentResult, OrderPlan, Outcome, Transfer
from hellabandz.trading.marketdata import MarketSnapshot

EXEC_AGENT = "exec"  # the single execution owner's ledger sub-account in every coordinated portfolio


@dataclass(frozen=True)
class RiskBudget:
    name: str
    max_order_notional: int  # per-order premium limit, micros
    max_committed: int  # cost basis held + cash reserved for working buys, micros
    daily_loss_limit: int  # micros below the day's opening equity at which buying stops
    max_instrument_bps: int = 3_000  # of equity, per instrument
    max_gross_bps: int = 9_000  # of equity, all positions + working buys
    group_caps: tuple[tuple[str, tuple[str, ...], int], ...] = ()  # (name, symbols, bps of equity)
    min_trade_qty: int = 1
    min_trade_notional: int = 0  # micros
    min_rebalance_bps: int = 1_000  # of a strategy's budget for the instrument
    cooldown_cycles: int = 2
    max_daily_turnover_bps: int = 20_000  # traded notional per UTC day, as bps of opening equity
    max_data_age_s: int = 4 * 3600
    max_intent_age_s: int = 3600
    replace_tolerance_bps: int = 2_000  # a working order within 20% of the needed size is kept
    tif: str = "IOC"
    fee_per_lot: int = 5_000  # micros, matches EngineConfig.fee_per_share


def paper_budget(equity: int) -> RiskBudget:
    """Default budget for the $10,000 simulated portfolio."""
    return RiskBudget("paper_10k", max_order_notional=equity * 2_500 // BPS, max_committed=equity * 9_000 // BPS,
                      daily_loss_limit=equity * 500 // BPS,
                      group_caps=(("us_large_cap_tech", ("AAPL", "MSFT", "NVDA", "QQQ"), 6_000),),
                      min_trade_notional=25 * 10**6)


def small_budget(equity: int) -> RiskBudget:
    """A tiny account (for example $5). Whole shares of these symbols cost more than the whole account,
    so this budget is expected to reject every buy with INSUFFICIENT_BUDGET_FOR_MIN_LOT. That is the
    honest answer; it does not pretend a $10,000 simulation describes a $5 user."""
    return RiskBudget("small_account", max_order_notional=equity * 2_000 // BPS, max_committed=equity * 8_000 // BPS,
                      daily_loss_limit=equity * 1_000 // BPS, max_instrument_bps=5_000, min_trade_notional=0)


@dataclass(frozen=True)
class WorkingOrder:
    order_id: str
    side: str
    remaining: int
    limit_px: int | None


@dataclass(frozen=True)
class AccountView:
    cash: int
    reserved_cash: int
    equity: int
    kill_switch: bool
    positions: dict[str, int]  # execution owner's lots per instrument
    basis: int  # execution owner's cost basis
    working: dict[str, WorkingOrder]
    fees: int
    slippage: int

    @classmethod
    def from_state(cls, s, agent: str = EXEC_AGENT) -> "AccountView":
        pos, basis = {}, 0
        for key, p in s.positions.items():
            a, sym = key.split("|", 1)
            if a == agent and p.qty:
                pos[sym] = p.qty
                basis += p.basis
        working = {o.symbol: WorkingOrder(o.order_id, o.side, o.remaining, o.limit_px)
                   for o in sorted(s.orders.values(), key=lambda o: o.created_seq) if o.agent_id == agent}
        return cls(s.cash, s.reserved_cash, s.equity(), s.kill_switch, pos, basis, working, s.fees_paid, s.slippage_cost)


@dataclass(frozen=True)
class ActiveStrategy:
    strategy_id: str
    version: str
    universe: tuple[str, ...]
    capital: int  # sleeve capital, micros
    hold: bool = False  # deploy the budget once at cost; never trim while the target stays long

    def budget(self) -> int:
        return self.capital // len(self.universe)


@dataclass
class CoordinatorState:
    cycle: int = 0
    last_intent_ts: dict[tuple[str, str], int] = field(default_factory=dict)
    last_order_cycle: dict[str, int] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)  # submitted, not yet acknowledged by the ledger
    day: str = ""
    day_open_equity: int = 0
    traded_today: int = 0


def _round_up(x: int, tick: int) -> int:
    return -(-x // tick) * tick


class Coordinator:
    def __init__(self, budget: RiskBudget, instruments: dict[str, Instrument], venue: str, run_id: str):
        self.budget = budget
        self.instruments = instruments
        self.venue = venue
        self.run_id = run_id
        self.state = CoordinatorState()

    def _new_day(self, ts: int, equity: int) -> None:
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        if day != self.state.day:
            self.state.day, self.state.day_open_equity, self.state.traded_today = day, equity, 0

    def decide(self, snap: MarketSnapshot, intents: list[Intent], acct: AccountView, book: AttributionBook,
               active: dict[str, ActiveStrategy]) -> list[Decision]:
        st, b = self.state, self.budget
        st.cycle += 1
        self._new_day(snap.ts, acct.equity)
        portfolio_reasons = []
        buys_blocked = None
        if acct.kill_switch:
            buys_blocked = "KILL_SWITCH"
        elif st.day_open_equity - acct.equity >= b.daily_loss_limit:
            buys_blocked = "DAILY_LOSS_LIMIT"
        if buys_blocked:
            portfolio_reasons.append(buys_blocked)

        # ---- 2. validation: keep the latest valid intent per (strategy, instrument)
        by_sym: dict[str, dict[str, Intent]] = {}
        rejected: dict[str, list[IntentResult]] = {}
        seen = set()
        for it in sorted(intents, key=lambda i: (i.instrument, i.strategy_id, i.decision_ts, i.intent_id)):
            reason = None
            a = active.get(it.strategy_id)
            inst = self.instruments.get(it.instrument)
            key = (it.strategy_id, it.instrument)
            if it.intent_id in seen:
                reason = "DUPLICATE_INTENT"
            elif a is None:
                reason = "UNKNOWN_STRATEGY"
            elif it.strategy_version != a.version:
                reason = "INACTIVE_STRATEGY_VERSION"
            elif it.instrument not in a.universe:
                reason = "INSTRUMENT_OUTSIDE_UNIVERSE"
            elif inst is None:
                reason = "UNKNOWN_INSTRUMENT"
            elif inst.asset_class not in COORDINATED_ASSET_CLASSES:
                reason = "UNSUPPORTED_ASSET_CLASS"
            elif it.venue != self.venue:
                reason = "VENUE_MISMATCH"
            elif it.md_seq > snap.gateway_seq or it.decision_ts > snap.ts:
                reason = "INTENT_FROM_THE_FUTURE"
            elif it.decision_ts < st.last_intent_ts.get(key, 0):
                reason = "OUT_OF_ORDER_INTENT"
            elif it.expires_ts < snap.ts:
                reason = "INTENT_EXPIRED"
            elif snap.ts - it.decision_ts > b.max_intent_age_s:
                reason = "STALE_INTENT"
            seen.add(it.intent_id)
            if reason:
                rejected.setdefault(it.instrument, []).append(IntentResult(
                    intent_id=it.intent_id, strategy_id=it.strategy_id, outcome=Outcome.REJECTED, requested_qty=0,
                    attributed_qty=book.qty(it.strategy_id, it.instrument) if it.strategy_id in book.sleeves else 0,
                    approved_delta=0, reasons=[reason]))
                continue
            st.last_intent_ts[key] = it.decision_ts
            by_sym.setdefault(it.instrument, {})[it.strategy_id] = it

        decisions = []
        cycle_spend = 0  # cash this cycle's earlier buy orders will reserve
        cycle_buy_notional: dict[str, int] = {}
        for sym in sorted(set(by_sym) | set(rejected)):
            decisions.append(self._decide_instrument(snap, sym, by_sym.get(sym, {}), rejected.get(sym, []), acct, book,
                                                     active, buys_blocked, portfolio_reasons, cycle_spend,
                                                     cycle_buy_notional))
            d = decisions[-1]
            if d.order is not None and d.order.side == "BUY":
                cost = d.order.qty * (d.order.limit_px + b.fee_per_lot)
                cycle_spend += cost
                cycle_buy_notional[sym] = d.order.qty * d.price
        return decisions

    # ------------------------------------------------------------------ one instrument

    def _decide_instrument(self, snap, sym, intents, rejected, acct, book, active, buys_blocked, portfolio_reasons,
                           cycle_spend, cycle_buy_notional) -> Decision:
        b, st = self.budget, self.state
        view = snap.instruments.get(sym)
        price = view.last_close if view else 0
        current = acct.positions.get(sym, 0)
        results: dict[str, IntentResult] = {}
        reasons: dict[str, list[str]] = {k: [] for k in intents}

        def finish(order=None, transfers=(), cancel=None, extra=(), targets=None):
            out = list(rejected)
            for k in sorted(intents):
                if k in results:
                    out.append(results[k])
            tgt = current + (order.qty if order and order.side == "BUY" else -(order.qty if order else 0))
            dec = Decision(cycle=st.cycle, ts=snap.ts, instrument=sym, venue=self.venue, price=price, current_qty=current,
                           aggregate_target_qty=tgt, intents=out, transfers=list(transfers), order=order,
                           cancel_order_id=cancel, portfolio_reasons=list(portfolio_reasons) + list(extra),
                           explanation="")
            return dec.model_copy(update={"explanation": explain(dec)})

        def mark_all(outcome, reason, desired=None):
            for k, it in intents.items():
                results[k] = IntentResult(intent_id=it.intent_id, strategy_id=k, outcome=outcome,
                                          requested_qty=(desired or {}).get(k, 0), attributed_qty=book.qty(k, sym),
                                          approved_delta=0, reasons=reasons[k] + [reason])

        if not intents:
            return finish()
        if view is None or not snap.fresh(sym, b.max_data_age_s) or price <= 0:
            mark_all(Outcome.REJECTED, "STALE_DATA")
            return finish(extra=["STALE_DATA"])

        # ---- 4. desired lots per strategy, with the rebalance band
        desired: dict[str, int] = {}
        for k, it in intents.items():
            budget = active[k].budget()
            have = book.qty(k, sym)
            if active[k].hold:
                # same rule as the research backtester's hold slots: top up toward the cost budget, never trim
                room = budget * it.target_weight_bps // BPS - book.basis(k, sym)
                add = room // price if it.target_weight_bps and room * BPS >= budget * b.min_rebalance_bps else 0
                desired[k] = have + max(0, add) if it.target_weight_bps else 0
                if desired[k] == have and it.target_weight_bps:
                    reasons[k].append("HOLDING")
                continue
            want = budget * it.target_weight_bps // BPS // price
            if want != 0 and abs(want - have) * price * BPS < budget * b.min_rebalance_bps:
                reasons[k].append("WITHIN_REBALANCE_BAND")
                want = have
            desired[k] = want
        attributed = {k: book.qty(k, sym) for k in intents}

        # ---- 5. internal netting
        surplus = {k: attributed[k] - desired[k] for k in sorted(intents) if attributed[k] > desired[k]}
        shortfall = {k: desired[k] - attributed[k] for k in sorted(intents) if desired[k] > attributed[k]}
        transfers = []
        netted: dict[str, int] = {}
        for src in list(surplus):
            for dst in list(shortfall):
                q = min(surplus[src], shortfall.get(dst, 0))
                if q <= 0:
                    continue
                transfers.append(Transfer(instrument=sym, from_strategy=src, to_strategy=dst, qty=q, px=price))
                surplus[src] -= q
                shortfall[dst] -= q
                netted[src] = netted.get(src, 0) - q
                netted[dst] = netted.get(dst, 0) + q
        surplus = {k: v for k, v in surplus.items() if v > 0}
        shortfall = {k: v for k, v in shortfall.items() if v > 0}
        need = sum(shortfall.values()) - sum(surplus.values())  # one side is zero after netting

        def settle(order=None, extra=(), cancel=None, approved=None):
            approved = approved or {}
            for k, it in intents.items():
                n, a = netted.get(k, 0), approved.get(k, 0)
                if order is not None and order.side == "SELL":
                    a = -a
                gap = desired[k] - attributed[k]
                if gap == 0:
                    out, why = Outcome.ACCEPTED, ["AT_TARGET"]
                elif n and n == gap:
                    out, why = Outcome.NETTED, ["NETTED_INTERNALLY"]
                elif order is not None and n + a == gap:
                    out, why = Outcome.ACCEPTED, []
                elif order is not None and a != 0:
                    out, why = Outcome.REDUCED, list(extra)
                else:
                    out, why = Outcome.DEFERRED, list(extra) or ["NO_ORDER"]
                results[k] = IntentResult(intent_id=it.intent_id, strategy_id=k, outcome=out, requested_qty=desired[k],
                                          attributed_qty=attributed[k], approved_delta=n + a, reasons=reasons[k] + why)
            return finish(order=order, transfers=transfers, cancel=cancel, extra=extra)

        if need == 0:
            return settle()

        # ---- 6. order lifecycle
        working = acct.working.get(sym)
        if any(oid.startswith(f"{self.run_id}.{sym}.") for oid in st.pending):
            return settle(extra=["PENDING_SUBMISSION"])
        if working is not None:
            side = "BUY" if need > 0 else "SELL"
            tol = max(b.min_trade_qty, working.remaining * b.replace_tolerance_bps // BPS)
            if working.side == side and abs(working.remaining - abs(need)) <= tol:
                return settle(extra=["KEEP_WORKING_ORDER"])
            return settle(extra=["REPLACE_AFTER_CANCEL"], cancel=working.order_id)

        inst = self.instruments[sym]
        target_after = current + need
        if st.cycle - st.last_order_cycle.get(sym, -10**9) < b.cooldown_cycles and target_after != 0:
            return settle(extra=["COOLDOWN"])

        limits: list[str] = []
        if need > 0:
            slip = min(intents[k].max_slippage_bps for k in shortfall)
            limit = _round_up(price * (BPS + slip) // BPS + 1, inst.tick)
            per_lot = limit + b.fee_per_lot
            qty = need
            if buys_blocked:
                return settle(extra=[buys_blocked])
            equity = acct.equity
            caps = [
                (b.max_order_notional // limit, "MAX_ORDER_NOTIONAL"),
                ((b.max_committed - acct.basis - acct.reserved_cash - cycle_spend) // per_lot, "MAX_COMMITTED_CAPITAL"),
                ((equity * b.max_instrument_bps // BPS - current * price) // price, "MAX_INSTRUMENT_EXPOSURE"),
                ((equity * b.max_gross_bps // BPS - (acct.equity - acct.cash) - acct.reserved_cash - cycle_spend) // per_lot,
                 "MAX_GROSS_EXPOSURE"),
                ((acct.cash - acct.reserved_cash - cycle_spend) // per_lot, "BUYING_POWER"),
                ((st.day_open_equity * b.max_daily_turnover_bps // BPS - st.traded_today) // limit, "DAILY_TURNOVER"),
            ]
            for gname, gsyms, gbps in b.group_caps:
                if sym in gsyms:
                    group_mv = sum(acct.positions.get(s, 0) * snap.instruments[s].last_close
                                   for s in gsyms if s in snap.instruments) + sum(cycle_buy_notional.get(s, 0) for s in gsyms)
                    caps.append(((equity * gbps // BPS - group_mv) // price, f"GROUP_CAP:{gname}"))
            capped = [(max(0, c), code) for c, code in caps]
            qty = min([need] + [c for c, _ in capped])
            limits = [code for c, code in capped if c == qty and qty < need]
            if qty == 0 and (per_lot > b.max_order_notional or per_lot > b.max_committed):
                return settle(extra=["INSUFFICIENT_BUDGET_FOR_MIN_LOT"])
            plan_from = shortfall
        else:
            slip = min(intents[k].max_slippage_bps for k in surplus)
            limit = max(inst.tick, (price * (BPS - slip) // BPS) // inst.tick * inst.tick)
            qty = min(-need, current)
            plan_from = surplus
        if qty <= 0:
            return settle(extra=limits or ["NO_CAPACITY"])
        if (qty < b.min_trade_qty or qty * price < b.min_trade_notional) and target_after != 0:
            return settle(extra=["BELOW_MIN_TRADE"])

        attribution = split(qty, plan_from) if qty < sum(plan_from.values()) else dict(plan_from)
        oid = f"{self.run_id}.{sym}.{st.cycle}"
        order = OrderPlan(client_order_id=oid, instrument=sym, side="BUY" if need > 0 else "SELL", qty=qty,
                          limit_px=limit, tif=b.tif, attribution=attribution)
        st.last_order_cycle[sym] = st.cycle
        st.traded_today += qty * price
        return settle(order=order, extra=limits, approved=attribution)


def explain(d: Decision) -> str:
    """One sentence from the recorded decision, for the UI."""
    asked = [r for r in d.intents if r.outcome != Outcome.REJECTED and r.requested_qty != r.attributed_qty]
    n_rej = sum(r.outcome == Outcome.REJECTED for r in d.intents)
    parts = []
    if asked:
        wants = ", ".join(f"{r.strategy_id} {r.requested_qty - r.attributed_qty:+d}" for r in asked)
        parts.append(f"{len(asked)} agent{'s' if len(asked) != 1 else ''} requested a change in {d.instrument} ({wants} lots)")
    else:
        parts.append(f"No agent asked to change {d.instrument}")
    if d.transfers:
        parts.append(f"the coordinator netted {sum(t.qty for t in d.transfers)} lots between strategies without trading")
    if d.order is not None:
        cut = [x for x in d.portfolio_reasons if x not in ("KILL_SWITCH", "DAILY_LOSS_LIMIT")]
        parts.append(f"it approved one aggregate {d.order.side} of {d.order.qty} at a limit of "
                     f"{d.order.limit_px / 1e6:.2f}" + (f", limited by {', '.join(cut)}" if cut else " within the portfolio budget"))
    elif asked and not d.transfers:
        why = [x for x in d.portfolio_reasons] or sorted({x for r in d.intents for x in r.reasons})
        parts.append("no order was sent" + (f" ({', '.join(why)})" if why else ""))
    if d.cancel_order_id:
        parts.append(f"working order {d.cancel_order_id} is being cancelled first")
    if n_rej:
        parts.append(f"{n_rej} intent{'s' if n_rej != 1 else ''} rejected")
    return "; ".join(parts) + "."
