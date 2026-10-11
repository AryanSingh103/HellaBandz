"""Instrument definitions. Each asset class has its own accounting assumptions; nothing is routed
through another class's model.

- EQUITY / ETF: whole shares, prices in micros on a one-cent tick, long-only cash account, the most
  a buyer can lose is what was paid plus fees. This is what the coordinator trades today.
- CRYPTO: base lots from the dataset (see ingestion.dataset unit_version base-lot-v2). Supported by
  the ledger and backtester, but the coordinator does not trade it yet.
- EVENT_CONTRACT (for example Kalshi): price is the cost of a contract that pays a fixed amount if
  the event happens; it settles, it does not trade forever. It needs its own strategies and a
  settlement model, so the coordinator refuses it rather than treating it like a stock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hellabandz.contracts import MICROS


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    EVENT_CONTRACT = "event_contract"


COORDINATED_ASSET_CLASSES = frozenset({AssetClass.EQUITY, AssetClass.ETF})


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: AssetClass
    tick: int  # micros
    lot: int  # execution lots per order increment
    quote_currency: str = "USD"
    settlement: str = "cash account, simulated immediate settlement"
    max_loss: str = "premium paid plus fees (long-only, no margin)"
    payout: int | None = None  # EVENT_CONTRACT only: micros paid per contract if the event happens


SECTOR_ETFS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
KNOWN_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA", "VTI", *SECTOR_ETFS})


def equity(symbol: str) -> Instrument:
    return Instrument(symbol, AssetClass.ETF if symbol in KNOWN_ETFS else AssetClass.EQUITY, tick=MICROS // 100, lot=1)


def event_contract(ticker: str) -> Instrument:
    return Instrument(ticker, AssetClass.EVENT_CONTRACT, tick=MICROS // 100, lot=1, settlement="binary, settles at expiry",
                      max_loss="price paid per contract plus fees", payout=MICROS)


def instruments_for_dataset(dataset: dict) -> dict[str, Instrument]:
    """Instrument table for a registered snapshot. Crypto datasets are labeled crypto, not equity."""
    crypto = dataset.get("unit_version") == "base-lot-v2" or dataset.get("quote_currency") == "USDT"
    out = {}
    for sym in dataset.get("symbols", []):
        out[sym] = (Instrument(sym, AssetClass.CRYPTO, tick=1, lot=1, quote_currency=dataset.get("quote_currency", "USDT"),
                               settlement="spot, simulated immediate settlement")
                    if crypto else equity(sym))
    return out
