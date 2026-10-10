"""Versioned message contracts shared by every QuantSwarm component.

Money is integer micro-dollars (1 USD = 1_000_000 micros). Quantities are whole
shares. Timestamps are timezone-aware UTC. Nothing in the financial core uses
floats.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"
MICROS = 1_000_000


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return ts.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_utc)]
Ident = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.:\-]+$")]


def usd_to_micros(x: str | int | float) -> int:
    """Convert a decimal dollar string (or number) to micros with round-half-even."""
    from decimal import ROUND_HALF_EVEN, Decimal

    return int((Decimal(str(x)) * MICROS).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def micros_to_usd(m: int) -> float:
    return m / MICROS


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MKT = "MKT"
    LMT = "LMT"


class Outcome(StrEnum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


class Bar(_Frozen):
    symbol: Ident
    ts: UtcDatetime  # bar open time; the bar is complete at ts + interval
    open: int
    high: int
    low: int
    close: int
    volume: int
    stress: bool = False  # set by the crash overlay; execution uses stress slippage


class OrderRequest(_Frozen):
    """What an agent (or human) asks for. Deliberately loose on numeric ranges so
    that invalid values reach the risk engine and get a recorded rejection."""

    client_order_id: Ident
    agent_id: Ident
    symbol: Ident
    side: Side
    qty: int
    order_type: OrderType = OrderType.MKT
    limit_px: int | None = None
    signal_bar_ts: UtcDatetime


# ---- State-machine commands (the only inputs that can change the ledger) ----


class OpenPortfolio(_Frozen):
    kind: Literal["open_portfolio"] = "open_portfolio"
    initial_cash: int = Field(gt=0)


class SetAllocation(_Frozen):
    kind: Literal["set_allocation"] = "set_allocation"
    agent_id: Ident
    capital: int = Field(ge=0)


class SubmitOrder(_Frozen):
    kind: Literal["submit_order"] = "submit_order"
    order: OrderRequest


class CancelOrder(_Frozen):
    kind: Literal["cancel_order"] = "cancel_order"
    client_order_id: Ident
    agent_id: Ident


class ApplyBar(_Frozen):
    kind: Literal["apply_bar"] = "apply_bar"
    bar: Bar


class SetKillSwitch(_Frozen):
    kind: Literal["set_kill_switch"] = "set_kill_switch"
    on: bool
    reason: str = Field(default="manual", max_length=200)


Command = Annotated[
    Union[OpenPortfolio, SetAllocation, SubmitOrder, CancelOrder, ApplyBar, SetKillSwitch],
    Field(discriminator="kind"),
]


class CommandEnvelope(_Frozen):
    command_id: Ident
    portfolio_id: Ident = "main"
    command: Command


def command_id_for(cmd: Any) -> str:
    """Natural idempotency key for each command type."""
    if isinstance(cmd, SubmitOrder):
        return f"order:{cmd.order.client_order_id}"
    if isinstance(cmd, ApplyBar):
        return f"bar:{cmd.bar.symbol}:{int(cmd.bar.ts.timestamp())}"
    if isinstance(cmd, CancelOrder):
        return f"cancel:{cmd.client_order_id}"
    raise ValueError("command type has no natural key; supply command_id explicitly")


# ---- Research / tournament contracts ----


class StrategyFamily(StrEnum):
    SMA_CROSSOVER = "sma_crossover"
    BREAKOUT = "breakout"
    ZSCORE_REVERSION = "zscore_reversion"
    VOL_TARGET = "vol_target"


class StrategyProposal(_Frozen):
    proposal_id: Ident
    agent_id: Ident
    family: StrategyFamily
    universe: list[Ident] = Field(min_length=1, max_length=10)
    param_grid: dict[str, list[float]] = Field(description="candidate values per parameter")
    rationale: str = Field(max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    schema_version: str = SCHEMA_VERSION


class StrategySpec(_Frozen):
    strategy_id: Ident
    agent_id: Ident
    family: StrategyFamily
    params: dict[str, float]
    universe: list[Ident]
    version: str = SCHEMA_VERSION


class Metrics(_Frozen):
    net_return: float
    sharpe: float
    max_drawdown: float
    n_fills: int
    turnover: float
    fees_usd: float
    slippage_usd: float
    final_equity_usd: float


class EvalResult(_Frozen):
    strategy_id: Ident
    split: Literal["train", "validation", "test"]
    metrics: Metrics
    trades_digest: str
    state_digest: str
