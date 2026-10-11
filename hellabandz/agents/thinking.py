"""What an agent is "thinking" during a session, for display only.

Two kinds of record, neither of which can move money:
- a rule reading: the indicator values behind the fixed rule's latest target, computed from the same bars;
- an AI commentary: a short LLM remark on facts the session hands it.

The target weight always comes from `hellabandz.strategies`; nothing here feeds back into trading.
"""

from __future__ import annotations

import math

import numpy as np

from hellabandz.strategies import BARS_PER_YEAR, Series

HOW = {
    "sma_crossover": "hold while the short-term average price is above the long-term average",
    "breakout": "buy when price breaks above its recent high, exit when it falls below its recent low",
    "zscore_reversion": "buy when price is unusually far below its recent average, exit once it recovers",
    "vol_target": "hold only while price is above its longer-term average, sized to keep volatility near a target",
    "rsi_reversion": "buy after heavy selling (low RSI), exit once price has bounced",
    "ts_momentum": "hold only while the return over the lookback window is above a threshold",
    "regime_reversion": "hold by default; in a range-bound market step aside when price is stretched above average",
    "buy_hold": "buy every symbol equally and hold",
}

THINK_SYSTEM = """You are the "{agent}" trading agent in a paper-trading simulation with pretend money.
Your trading rule is fixed code ({family}: {how}). You cannot place, size or cancel orders yourself, and an
independent risk engine checks every order your rule sends.
Using ONLY the JSON facts provided, say in one or two short plain-English sentences what you are seeing right now
and what your rule is doing about it. Mention a symbol and a number from the facts. Do not invent prices, news,
events or predictions. If "funded" is false you have no budget and are only watching.
Respond with JSON: {{"thought": str, "stance": "adding" | "holding" | "trimming" | "waiting"}}"""


def reading(family: str, params: dict[str, float], s: Series) -> str:
    """Plain-English indicator values for the latest bar of one symbol. Empty when the family has no simple reading."""
    c = s.close
    if len(c) < 3:
        return "not enough history yet"
    px = float(c[-1])
    try:
        if family == "sma_crossover":
            fast, slow = int(params["fast"]), int(params["slow"])
            if len(c) < slow:
                return "not enough history yet"
            f, sl = float(c[-fast:].mean()), float(c[-slow:].mean())
            return f"{fast}-bar average {f:,.2f} is {'above' if f > sl else 'below'} the {slow}-bar average {sl:,.2f}"
        if family == "breakout":
            n = int(params["lookback"])
            exit_n = max(2, n // 2)
            if len(c) <= n:
                return "not enough history yet"
            hi, lo = float(s.high[-n - 1:-1].max()), float(s.low[-exit_n - 1:-1].min())
            return f"price {px:,.2f} against the {n}-bar high {hi:,.2f} and the {exit_n}-bar low {lo:,.2f}"
        if family == "zscore_reversion":
            n = int(params["lookback"])
            if len(c) < n:
                return "not enough history yet"
            m, sd = float(c[-n:].mean()), float(c[-n:].std())
            z = (px - m) / sd if sd > 0 else 0.0
            return (f"price {px:,.2f} is {z:+.2f} standard deviations from its {n}-bar average {m:,.2f} "
                    f"(buys below {-params['entry_z']:+.2f}, exits above {params['exit_z']:+.2f})")
        if family == "vol_target":
            vn, tn = int(params["vol_lookback"]), int(params["trend_lookback"])
            if len(c) <= max(vn, tn):
                return "not enough history yet"
            vol = float(np.diff(np.log(c[-vn - 1:])).std()) * math.sqrt(BARS_PER_YEAR)
            trend = float(c[-tn:].mean())
            return (f"price {px:,.2f} is {'above' if px > trend else 'below'} its {tn}-bar average {trend:,.2f}; "
                    f"annualized volatility {vol:.0%} against a {params['target_vol']:.0%} target")
        if family == "rsi_reversion":
            n = int(params["lookback"])
            if len(c) <= n:
                return "not enough history yet"
            d = np.diff(c[-n - 1:])
            gain, loss = float(np.maximum(d, 0).mean()), float(np.maximum(-d, 0).mean())
            rsi = 100 - 100 / (1 + gain / loss) if loss > 0 else 100.0
            return f"RSI {rsi:.0f} (buys below {params['entry_rsi']:.0f}, exits above {params['exit_rsi']:.0f})"
        if family == "ts_momentum":
            n = int(params["lookback"])
            if len(c) <= n:
                return "not enough history yet"
            ret = px / float(c[-1 - n]) - 1
            return f"{n}-bar return {ret:+.1%} against a {params['threshold']:+.1%} threshold"
    except (KeyError, ValueError, FloatingPointError):
        return ""
    return ""
