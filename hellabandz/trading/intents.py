"""What strategies send the coordinator, and what the coordinator records about it.

A strategy never sends an order. It sends an Intent: "for this instrument, I would like my sleeve to
hold this much, for these reasons, based on this market data." The coordinator turns all intents for
an instrument into at most one order.

Scores are *not* comparable across strategies. A trend score and a mean-reversion score are on
different scales and are not calibrated probabilities, so the coordinator never weights one
strategy against another by score. Capital between strategies is set by their research allocation;
the score is kept for explanation and for the strategy's own sizing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from hellabandz.contracts import Ident


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Intent(_Frozen):
    intent_id: Ident
    strategy_id: Ident
    strategy_version: str = Field(min_length=1, max_length=64)
    instrument: Ident
    venue: Ident
    md_seq: int = Field(ge=0)  # gateway sequence of the snapshot the decision used
    decision_ts: int  # epoch seconds of the latest bar in that snapshot
    target_weight_bps: int = Field(ge=0, le=10_000)  # share of the strategy's budget for this instrument
    score: float
    score_meaning: str = Field(max_length=200)
    holding_bars: int = Field(ge=1)  # expected holding period
    max_slippage_bps: int = Field(ge=0, le=1_000)  # worst price the strategy accepts, relative to last close
    expires_ts: int  # ignore the intent after this time
    features: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list, max_length=10)


class Outcome(StrEnum):
    ACCEPTED = "accepted"  # contributes in full to the aggregate order
    REDUCED = "reduced"  # contributes, but a portfolio limit cut it down
    NETTED = "netted"  # satisfied internally against another strategy, no market order needed
    DEFERRED = "deferred"  # valid, but no order this cycle (working order, cooldown, below minimum size)
    REJECTED = "rejected"  # invalid, stale, out of order, or not allowed


class IntentResult(_Frozen):
    intent_id: str
    strategy_id: str
    outcome: Outcome
    requested_qty: int  # lots the strategy's sleeve would hold if fully honored
    attributed_qty: int  # lots the sleeve holds before this cycle
    approved_delta: int  # lots of the aggregate order (or internal transfer) attributed to this strategy
    reasons: list[str]


class OrderPlan(_Frozen):
    client_order_id: Ident
    instrument: Ident
    side: str
    qty: int
    limit_px: int
    tif: str
    attribution: dict[str, int]  # strategy -> planned lots; sums to qty


class Transfer(_Frozen):
    instrument: Ident
    from_strategy: Ident
    to_strategy: Ident
    qty: int
    px: int


class Decision(_Frozen):
    cycle: int
    ts: int
    instrument: Ident
    venue: Ident
    price: int
    current_qty: int  # account position before the decision
    aggregate_target_qty: int  # what the account will hold if the order fills completely
    intents: list[IntentResult]
    transfers: list[Transfer] = Field(default_factory=list)
    order: OrderPlan | None = None
    cancel_order_id: str | None = None
    portfolio_reasons: list[str] = Field(default_factory=list)
    explanation: str
