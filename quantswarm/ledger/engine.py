"""Deterministic trading state machine: risk checks + simulated fills.

`process(state, command, config)` is a pure function of (state, command,
config). It emits events through `ledger.state.append`, which is the only
mutation path. The same function powers the execution worker, the backtester,
and the tests, so a backtest and a live paper session share identical rules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from quantswarm.contracts import (
    ApplyBar,
    CancelOrder,
    OpenPortfolio,
    OrderRequest,
    SetAllocation,
    SetKillSwitch,
    SubmitOrder,
)
from quantswarm.ledger.state import PortfolioState, append, canonical, pos_key, sha256_hex

BPS = 10_000


@dataclass(frozen=True)
class EngineConfig:
    max_order_bps: int = 2_500  # R1: one order <= 25% of equity
    max_gross_bps: int = 9_000  # R3: positions + open buys <= 90% of equity
    limit_band_bps: int = 1_000  # R6: limit within +/-10% of last close
    max_signal_age_s: int = 2 * 3600  # R7: decision bar at most 2 hours older than latest bar
    kill_drawdown_bps: int = 1_500  # R9: trip kill switch at 15% drawdown from peak
    slippage_bps: int = 5
    stress_slippage_bps: int = 50
    reserve_buffer_bps: int = 200  # extra cash locked for market buys to absorb gaps
    fee_per_share: int = 5_000  # micros ($0.005/share)
    participation_bps: int = 100  # fill at most 1% of a bar's volume
    expiry_bars: int = 5

    def digest(self) -> str:
        return sha256_hex(canonical(asdict(self)))


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _payload_hash(o: OrderRequest) -> str:
    return sha256_hex(o.model_dump_json())


# --------------------------------------------------------------------------- risk


def evaluate_order(s: PortfolioState, o: OrderRequest, cfg: EngineConfig) -> tuple[int, list[str], int]:
    """Return (approved_qty, rule_codes, reserved_cash). approved_qty == 0 means reject."""
    if not s.opened:
        return 0, ["R0_PORTFOLIO_NOT_OPEN"], 0
    if o.symbol not in s.last_bar:
        return 0, ["R6_UNKNOWN_SYMBOL"], 0
    last_ts, last_close = s.last_bar[o.symbol]
    if o.qty <= 0:
        return 0, ["R6_INVALID_QTY"], 0
    if o.order_type == "LMT":
        if o.limit_px is None or o.limit_px <= 0:
            return 0, ["R6_INVALID_LIMIT"], 0
        if abs(o.limit_px - last_close) * BPS > last_close * cfg.limit_band_bps:
            return 0, ["R6_LIMIT_OUTSIDE_BAND"], 0
    elif o.limit_px is not None:
        return 0, ["R6_INVALID_LIMIT"], 0
    sig = int(o.signal_bar_ts.timestamp())
    if sig > last_ts:
        return 0, ["R7_FUTURE_SIGNAL"], 0
    if last_ts - sig > cfg.max_signal_age_s:
        return 0, ["R7_STALE_SIGNAL"], 0

    if o.side == "SELL":
        p = s.position(o.agent_id, o.symbol)
        avail = p.qty - p.reserved
        if avail <= 0:
            return 0, ["R5_NO_POSITION"], 0
        if o.qty > avail:
            return avail, ["R5_POSITION"], 0
        return o.qty, [], 0

    # BUY
    if s.kill_switch:
        return 0, ["R9_KILL_SWITCH"], 0
    ref = o.limit_px if o.order_type == "LMT" else _ceil_div(last_close * (BPS + cfg.reserve_buffer_bps), BPS)
    per_share = ref + cfg.fee_per_share
    equity = s.equity()
    caps: list[tuple[int, str]] = [
        (equity * cfg.max_order_bps // BPS // ref, "R1_MAX_ORDER_VALUE"),
        (max(0, s.allocations.get(o.agent_id, 0) - s.agent_exposure(o.agent_id)) // per_share, "R2_AGENT_ALLOCATION"),
        (max(0, equity * cfg.max_gross_bps // BPS - s.market_value() - s.reserved_cash) // per_share, "R3_GROSS_EXPOSURE"),
        ((s.cash - s.reserved_cash) // per_share, "R4_BUYING_POWER"),
    ]
    approved = min([o.qty] + [c for c, _ in caps])
    binding = [code for c, code in caps if c == approved and c < o.qty]  # tightest limits
    return approved, binding, approved * per_share


# --------------------------------------------------------------------------- fills


def fill_for_bar(o, bar, cfg: EngineConfig) -> tuple[int, int, int] | None:
    """Bar-based fill model. Returns (qty, px, slippage_cost) or None.

    - order must have been decided on an earlier bar (no same-bar fills)
    - MKT fills at the open +/- slippage (rounded against the trader)
    - LMT fills only if the bar trades through the limit, at the better of open/limit
    - quantity capped by participation_bps of bar volume and, for buys, by cash
      still reserved for the order (a gap up yields a partial fill, never debt)
    """
    if o.signal_ts >= int(bar.ts.timestamp()):
        return None
    slip = cfg.stress_slippage_bps if bar.stress else cfg.slippage_bps
    if o.order_type == "MKT":
        if o.side == "BUY":
            px = _ceil_div(bar.open * (BPS + slip), BPS)
        else:
            px = bar.open * (BPS - slip) // BPS
        slippage_per = abs(px - bar.open)
    else:
        if o.side == "BUY":
            if bar.low > o.limit_px:
                return None
            px = min(bar.open, o.limit_px)
        else:
            if bar.high < o.limit_px:
                return None
            px = max(bar.open, o.limit_px)
        slippage_per = 0
    qty = min(o.remaining, bar.volume * cfg.participation_bps // BPS)
    if o.side == "BUY":
        qty = min(qty, o.reserved_cash // (px + cfg.fee_per_share))
    elif px <= cfg.fee_per_share:
        return None
    if qty <= 0:
        return None
    return qty, px, qty * slippage_per


# --------------------------------------------------------------------------- dispatcher


def process(s: PortfolioState, cmd, cfg: EngineConfig) -> tuple[list[dict], dict]:
    """Apply one command. Returns (stamped events, response)."""
    ev: list[dict] = []

    def emit(e: dict) -> None:
        ev.append(append(s, e))

    if isinstance(cmd, SubmitOrder):
        o = cmd.order
        h = _payload_hash(o)
        prior = s.decisions.get(o.client_order_id)
        if prior is not None:
            if prior["h"] != h:
                return ev, {"status": "conflict", "rules": ["R8_DUPLICATE_ID_DIFFERENT_PAYLOAD"]}
            return ev, {"status": "duplicate", "outcome": prior["outcome"],
                        "approved_qty": prior["approved_qty"], "rules": prior["rules"]}
        approved, rules, reserve = evaluate_order(s, o, cfg)
        base = {"order_id": o.client_order_id, "agent_id": o.agent_id, "symbol": o.symbol,
                "side": o.side.value, "requested_qty": o.qty, "rules": rules, "payload_hash": h}
        if approved <= 0:
            emit({"type": "OrderRejected", **base})
            return ev, {"status": "rejected", "outcome": "REJECT", "approved_qty": 0, "rules": rules}
        outcome = "APPROVE" if approved == o.qty else "REDUCE"
        emit({"type": "OrderAccepted", **base, "outcome": outcome, "qty": approved,
              "order_type": o.order_type.value, "limit_px": o.limit_px, "reserved_cash": reserve,
              "signal_ts": int(o.signal_bar_ts.timestamp())})
        return ev, {"status": "accepted", "outcome": outcome, "approved_qty": approved, "rules": rules}

    if isinstance(cmd, ApplyBar):
        b = cmd.bar
        ts = int(b.ts.timestamp())
        if not (0 < b.low <= min(b.open, b.close) and max(b.open, b.close) <= b.high and b.volume >= 0):
            return ev, {"status": "rejected", "reason": "BAR_INVALID_OHLC"}
        prev = s.last_bar.get(b.symbol)
        if prev is not None and ts <= prev[0]:
            return ev, {"status": "rejected", "reason": "BAR_OUT_OF_ORDER"}
        emit({"type": "BarApplied", "symbol": b.symbol, "ts": ts, "close": b.close})
        n_fills = 0
        for o in sorted((o for o in s.orders.values() if o.symbol == b.symbol), key=lambda o: o.created_seq):
            f = fill_for_bar(o, b, cfg)
            if f is not None:
                q, px, slip_cost = f
                emit({"type": "OrderFilled", "order_id": o.order_id, "agent_id": o.agent_id,
                      "symbol": o.symbol, "side": o.side, "qty": q, "px": px,
                      "fee": q * cfg.fee_per_share, "slippage": slip_cost, "bar_ts": ts})
                n_fills += 1
            if o.remaining == 0:
                emit({"type": "OrderClosed", "order_id": o.order_id, "reason": "filled"})
            elif o.bars_seen >= cfg.expiry_bars:
                emit({"type": "OrderClosed", "order_id": o.order_id, "reason": "expired"})
        if not s.kill_switch and s.peak_equity > 0:
            if s.equity() * BPS <= s.peak_equity * (BPS - cfg.kill_drawdown_bps):
                _trip_kill_switch(s, "drawdown", emit)
        return ev, {"status": "applied", "fills": n_fills}

    if isinstance(cmd, CancelOrder):
        o = s.orders.get(cmd.client_order_id)
        if o is None or o.agent_id != cmd.agent_id:
            return ev, {"status": "not_found"}
        emit({"type": "OrderClosed", "order_id": o.order_id, "reason": "cancelled"})
        return ev, {"status": "cancelled"}

    if isinstance(cmd, SetKillSwitch):
        if cmd.on and not s.kill_switch:
            _trip_kill_switch(s, cmd.reason, emit)
        elif not cmd.on and s.kill_switch:
            emit({"type": "KillSwitchSet", "on": False, "reason": cmd.reason})
        return ev, {"status": "ok", "kill_switch": s.kill_switch}

    if isinstance(cmd, SetAllocation):
        if not s.opened:
            return ev, {"status": "rejected", "rules": ["R0_PORTFOLIO_NOT_OPEN"]}
        total = sum(v for k, v in s.allocations.items() if k != cmd.agent_id) + cmd.capital
        if total > s.equity():
            return ev, {"status": "rejected", "rules": ["R2_ALLOCATIONS_EXCEED_EQUITY"]}
        emit({"type": "AllocationSet", "agent_id": cmd.agent_id, "capital": cmd.capital})
        return ev, {"status": "ok"}

    if isinstance(cmd, OpenPortfolio):
        if s.opened:
            return ev, {"status": "rejected", "reason": "ALREADY_OPEN"}
        emit({"type": "PortfolioOpened", "initial_cash": cmd.initial_cash})
        return ev, {"status": "ok"}

    raise TypeError(f"unsupported command {type(cmd).__name__}")


def _trip_kill_switch(s: PortfolioState, reason: str, emit) -> None:
    emit({"type": "KillSwitchSet", "on": True, "reason": reason})
    for o in sorted((o for o in s.orders.values() if o.side == "BUY"), key=lambda o: o.created_seq):
        emit({"type": "OrderClosed", "order_id": o.order_id, "reason": "kill_switch"})


__all__ = ["EngineConfig", "evaluate_order", "fill_for_bar", "process", "pos_key"]
