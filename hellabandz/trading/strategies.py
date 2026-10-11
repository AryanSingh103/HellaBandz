"""Runtime strategy agents for the coordinated system.

Three executable strategies, each a reviewed, bounded implementation of an existing whitelisted
family (hellabandz.strategies), plus a research critic (hellabandz.trading.critic) that never trades.

Every bar, a strategy reads a market snapshot and emits one Intent per instrument in its universe.
Indicators are updated incrementally: running sums over bounded windows (with an exact re-sum every
512 updates to stop floating-point drift), not a recomputation over the whole history. On start a
strategy is warmed once with the vectorized family function over the available history, so its
hysteresis state (for example "currently holding after a z-score entry") matches what the research
backtest computed. tests/unit/test_runtime_strategies.py checks the incremental signal against the
vectorized one bar by bar.

LLMs do not run here. Research may propose parameters (bounded by FAMILIES); this module only
executes reviewed code with those parameters.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass, field, replace

import numpy as np

from hellabandz.contracts import StrategyFamily
from hellabandz.ledger.state import canonical, sha256_hex
from hellabandz.strategies import (ALLOCATION_FAMILIES, BARS_PER_YEAR, BASKET_FAMILIES, FAMILIES, HOLD_FAMILIES, Series, _rolling_mean, _rolling_std,
                                   basket_index, reversion_speed, signal, validate_params)
from hellabandz.trading.intents import Intent
from hellabandz.trading.marketdata import InstrumentView, MarketSnapshot

CODE_VERSION = "runtime-regime-units-4"
REFRESH_EVERY = 512


@dataclass(frozen=True)
class StrategyCard:
    role: str
    family: StrategyFamily
    hypothesis: str
    data: str
    entry: str
    exit: str
    failure_conditions: str
    cost_assumptions: str
    score_meaning: str
    max_slippage_bps: int


COST_NOTE = ("fills at the documented model: bar-sim next open +/- 5 bps, or the paper CLOB synthetic ladder; "
             "fee $0.005/share; turnover is reported and capped by the coordinator")

CARDS: dict[str, StrategyCard] = {
    "trend": StrategyCard(
        "trend", StrategyFamily.SMA_CROSSOVER,
        "Large US ETFs and mega caps trend over weeks; holding while a fast moving average is above a slow one "
        "keeps exposure during those moves and steps aside during sustained declines.",
        "hourly closes for each symbol in the universe",
        "fast simple moving average rises above the slow one",
        "fast simple moving average falls back below the slow one",
        "whipsaw in range-bound markets; when fast and slow are close, costs exceed the edge; late exits after gaps",
        COST_NOTE, "(fast SMA - slow SMA) / slow SMA; positive means uptrend. Not comparable to other strategies.", 30),
    "mean_reversion": StrategyCard(
        "mean_reversion", StrategyFamily.ZSCORE_REVERSION,
        "Single large caps over-react on short horizons; buying when price is far below its rolling mean and "
        "selling once it has come back captures part of the snap-back.",
        "hourly closes for each symbol in the universe",
        "rolling z-score of price falls below -entry_z",
        "rolling z-score rises above exit_z",
        "trending markets where a cheap price keeps getting cheaper; regime changes; earnings gaps",
        COST_NOTE, "-z (how many standard deviations below the rolling mean). Not comparable to other strategies.", 30),
    "defensive": StrategyCard(
        "defensive", StrategyFamily.VOL_TARGET,
        "Exposure sized to a target volatility, and only above a long trend average, should give up less in "
        "turbulent, falling markets than holding a fixed amount.",
        "hourly closes for each symbol in the universe (log returns and a trend average)",
        "price above its trend average; size = target_vol / realized volatility, capped at fully invested",
        "price below its trend average, or volatility so high the size rounds to zero",
        "sharp rebounds after selloffs are missed; volatility estimates lag sudden regime changes",
        COST_NOTE, "target_vol / realized annualized volatility, capped at 1. Not comparable to other strategies.", 30),
    "regime": StrategyCard(
        "regime", StrategyFamily.REGIME_REVERSION,
        "Hold the equal-weight basket by default. Only when the basket's trailing mean-reversion speed says the "
        "market is range-bound, step aside once it is stretched above its rolling mean and buy back when it has "
        "come back. In trending or random-walk markets it never sells, so it is buy-and-hold plus a bounded bet.",
        "hourly closes of every symbol, combined into one equal-weight index",
        "invested by default; re-enter after a stretched range-bound basket falls back below rebuy_z",
        "basket z-score above sell_z while the reversion speed exceeds min_reversion",
        "trending markets misread as range-bound; regime estimates lag; costs of stepping out and back in",
        COST_NOTE, "basket z-score; the decision also needs the reversion speed. Not comparable to other strategies.", 30),
    "core": StrategyCard(
        "core", StrategyFamily.BUY_HOLD,
        "The passive baseline: buy an equal-weight basket once and hold it. Capital no strategy has earned stays here.",
        "none beyond the last close", "deploy once", "never, while running", "none: it is the benchmark",
        COST_NOTE, "always 1 (fully invested). Not comparable to other strategies.", 30),
}

for _role, _family, _hypothesis, _entry, _exit in (
    ("breakout", StrategyFamily.BREAKOUT, "Persistent price discovery can continue after a prior-range breakout.",
     "close above prior lookback highs", "close below prior half-lookback lows"),
    ("rsi", StrategyFamily.RSI_REVERSION, "An oversold liquid equity can rebound as temporary selling subsides.",
     "trailing RSI below entry_rsi", "trailing RSI above exit_rsi"),
    ("momentum", StrategyFamily.TS_MOMENTUM, "Positive trailing returns can persist over intermediate horizons.",
     "trailing lookback return above threshold", "trailing return falls below threshold"),
):
    CARDS[_role] = StrategyCard(_role, _family, _hypothesis, "hourly OHLC prices, available bars only",
                                _entry, _exit, "regime changes, gaps, whipsaw and trading costs can erase the edge",
                                COST_NOTE, "family-specific trailing signal; Not comparable to other strategies.", 30)

for _role, _family in (("rotation", StrategyFamily.RELATIVE_STRENGTH),
                        ("risk_rotation", StrategyFamily.RISK_ADJUSTED_ROTATION)):
    CARDS[_role] = StrategyCard(
        _role, _family, "Rank liquid sector ETFs by trailing relative strength; test persistence across sectors.",
        "completed New York session close proxies from available hourly bars; registered eligible universe",
        "daily or weekly top-k ranking; positive threshold; equal shares of the whole strategy sleeve",
        "rank falls out of top-k or trailing return fails threshold; unfilled slots remain cash",
        "momentum reversals, correlated sectors, missing data and costs; historical results do not establish an edge",
        COST_NOTE, "trailing session return, divided by annualized session volatility for risk-adjusted ranking", 30)
CARDS["regime_units"] = replace(CARDS["regime"], role="regime_units", family=StrategyFamily.REGIME_FIXED_UNITS,
    hypothesis=CARDS["regime"].hypothesis + " Preserve the initial intended integer share target after reentry, "
               "rather than shrinking appreciated holdings back to opening dollar capital.",
    failure_conditions=CARDS["regime"].failure_conditions + "; reacquisition can be cash/risk limited after an adverse exit")
FAMILY_CARDS = {card.family.value: role for role, card in CARDS.items()}


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    family: StrategyFamily
    params: tuple[tuple[str, float], ...]
    universe: tuple[str, ...]
    version: str
    card: StrategyCard = field(compare=False)

    @property
    def param_dict(self) -> dict[str, float]:
        return dict(self.params)

    def describe(self) -> dict:
        c = self.card
        return {"strategy_id": self.strategy_id, "version": self.version, "family": self.family.value,
                "params": self.param_dict, "universe": list(self.universe), "code_version": CODE_VERSION,
                "weight_scope": "sleeve" if self.family in ALLOCATION_FAMILIES else "slot",
                "hypothesis": c.hypothesis, "data": c.data, "entry": c.entry, "exit": c.exit,
                "parameter_bounds": FAMILIES[self.family][1], "failure_conditions": c.failure_conditions,
                "cost_assumptions": c.cost_assumptions, "score_meaning": c.score_meaning}


def make_version(strategy_id: str, role: str, params: dict[str, float], universe: list[str]) -> StrategyVersion:
    card = CARDS[role]
    validate_params(card.family, params)
    p = tuple(sorted((k, float(v)) for k, v in params.items()))
    u = tuple(sorted(universe))
    h = sha256_hex(canonical({"family": card.family.value, "params": dict(p), "universe": list(u), "code": CODE_VERSION}))
    return StrategyVersion(strategy_id, card.family, p, u, h[:16], card)


class RollingMean:
    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.s = 0.0
        self.k = 0

    def push(self, x: float) -> None:
        if len(self.buf) == self.n:
            self.s -= self.buf[0]
        self.buf.append(x)
        self.s += x
        self.k += 1
        if self.k % REFRESH_EVERY == 0:
            self.s = math.fsum(self.buf)

    @property
    def ready(self) -> bool:
        return len(self.buf) == self.n

    @property
    def value(self) -> float:
        return self.s / self.n


class RollingStd:
    """Population standard deviation (numpy ddof=0) over a window, from shifted running sums."""

    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.shift = 0.0
        self.s = 0.0
        self.s2 = 0.0
        self.k = 0

    def _resum(self) -> None:
        a = np.fromiter(self.buf, dtype=np.float64)
        self.shift = float(a[0]) if len(a) else 0.0
        d = a - self.shift
        self.s, self.s2 = float(d.sum()), float((d * d).sum())

    def push(self, x: float) -> None:
        if not self.buf:
            self.shift = x
        if len(self.buf) == self.n:
            old = self.buf[0] - self.shift
            self.s -= old
            self.s2 -= old * old
        self.buf.append(x)
        d = x - self.shift
        self.s += d
        self.s2 += d * d
        self.k += 1
        if self.k % REFRESH_EVERY == 0:
            self._resum()

    @property
    def ready(self) -> bool:
        return len(self.buf) == self.n

    @property
    def value(self) -> float:
        var = (self.s2 - self.s * self.s / self.n) / self.n
        return math.sqrt(var) if var > 0 else 0.0


def _bps(w: float) -> int:
    if not math.isfinite(w):
        return 0
    return int(min(max(np.rint(w * 10_000), 0), 10_000))


class _SymbolState:
    def __init__(self) -> None:
        self.last_ts = 0
        self.last_close = 0.0
        self.held = False
        self.weight_bps = 0
        self.score = 0.0
        self.features: dict[str, float] = {}
        self.reasons: list[str] = []
        self.ind: dict[str, object] = {}


class RuntimeStrategy:
    def __init__(self, version: StrategyVersion, venue: str):
        self.v = version
        self.venue = venue
        self.p = version.param_dict
        self.state = {sym: _SymbolState() for sym in version.universe}
        if self.allocation:
            from hellabandz.trading.allocation import AllocationState
            self.allocator = AllocationState(version)

    # ------------------------------------------------------------------ indicators

    @property
    def allocation(self) -> bool:
        return self.v.family in ALLOCATION_FAMILIES

    @property
    def hold(self) -> bool:
        return self.v.family in HOLD_FAMILIES

    @property
    def basket(self) -> bool:
        return self.v.family in BASKET_FAMILIES

    @property
    def fixed_units(self) -> bool:
        return self.v.family == StrategyFamily.REGIME_FIXED_UNITS

    def warm_universe(self, histories: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]) -> None:
        """histories: sym -> (ts, close, high, low), strictly before the evaluated period."""
        if self.allocation:
            self.allocator.warm(histories)
            return
        if not self.basket:
            for sym, (ts, c, h, lo) in histories.items():
                if sym in self.state:
                    self.warm(sym, ts, c, h, lo)
            return
        p = self.p
        members = {s: (Series(c, h, lo), ts) for s, (ts, c, h, lo) in histories.items() if s in self.state and len(ts)}
        index, its = basket_index(members) if len(members) == len(self.state) else (Series(np.array([]), np.array([]), np.array([])), np.array([]))
        if not len(its):
            members = {}
        self._base = {}
        start_ts = int(its[0]) if len(its) else 0
        for s, (series, ts) in members.items():
            k = int(np.searchsorted(ts, start_ts, side="right")) - 1
            self._base[s] = math.log(float(series.close[max(k, 0)]))
        self._lvl = RollingMean(int(p["lookback"]))
        self._lvl_sd = RollingStd(int(p["lookback"]))
        self._loglvl: deque[float] = deque(maxlen=int(p["regime_lookback"]) + 1)
        self._last = {s: float(members[s][0].close[-1]) for s in members}
        self._feat = {"basket_z": 0.0, "reversion_speed": 0.0}
        self._rich = False
        if len(index.close):
            lvl = index.close
            n = int(p["lookback"])
            m, sd = _rolling_mean(lvl, n), _rolling_std(lvl, n)
            z = np.where(sd > 0, (lvl - m) / np.where(sd > 0, sd, 1.0), 0.0)
            for zi in z:  # replay the path-dependent state exactly as the research function does
                if not self._rich and zi > p["sell_z"]:
                    self._rich = True
                elif self._rich and zi < p["rebuy_z"]:
                    self._rich = False
            for x in lvl[-n:]:
                self._lvl.push(float(x))
                self._lvl_sd.push(float(x))
            for x in np.log(lvl[-self._loglvl.maxlen:]):
                self._loglvl.append(float(x))
        w = int(signal(self.v.family, p, index)[-1]) if len(index.close) else 10_000
        for s in self.v.universe:
            st = self.state[s]
            st.ind = {"basket": True}
            st.weight_bps = w
            if s in members:
                st.last_ts = int(members[s][1][-1])
                st.last_close = self._last[s]
            st.reasons = ["HOLD" if w else "STEPPED_ASIDE"]

    def _basket_update(self, closes: dict[str, float]) -> int:
        p = self.p
        self._last.update(closes)
        for s in self.v.universe:  # no history before the run: rebase on the first bar seen
            if s not in self._base and s in self._last:
                self._base[s] = math.log(self._last[s])
        if len(self._base) < len(self.v.universe):
            return 10_000
        level = math.exp(sum(math.log(self._last[s]) - self._base[s] for s in self._base) / len(self._base))
        self._lvl.push(level)
        self._lvl_sd.push(level)
        self._loglvl.append(math.log(level))
        sd = self._lvl_sd.value
        z = (level - self._lvl.value) / sd if self._lvl.ready and sd > 0 else 0.0
        speed = float("nan")
        if len(self._loglvl) == self._loglvl.maxlen:
            a = np.fromiter(self._loglvl, dtype=np.float64)
            x = a[:-1] - a[:-1].mean()
            den = float((x * x).sum())
            speed = -float((x * np.diff(a)).sum()) / den if den > 0 else float("nan")
        if not self._rich and z > p["sell_z"]:
            self._rich = True
        elif self._rich and z < p["rebuy_z"]:
            self._rich = False
        w = 0 if self._rich and speed > p["min_reversion"] else 10_000
        self._feat = {"basket_z": z, "reversion_speed": speed if math.isfinite(speed) else 0.0}
        return w

    def _indicators(self) -> dict[str, object]:
        f, p = self.v.family, self.p
        if f == StrategyFamily.BUY_HOLD:
            return {"hold": True}
        if f == StrategyFamily.SMA_CROSSOVER:
            return {"fast": RollingMean(int(p["fast"])), "slow": RollingMean(int(p["slow"]))}
        if f == StrategyFamily.ZSCORE_REVERSION:
            n = int(p["lookback"])
            return {"mean": RollingMean(n), "std": RollingStd(n)}
        if f == StrategyFamily.VOL_TARGET:
            return {"vol": RollingStd(int(p["vol_lookback"])), "trend": RollingMean(int(p["trend_lookback"]))}
        if f == StrategyFamily.BREAKOUT:
            n = int(p["lookback"])
            return {"high": deque(maxlen=n), "low": deque(maxlen=max(2, n // 2))}
        if f == StrategyFamily.TS_MOMENTUM:
            return {"close": deque(maxlen=int(p["lookback"]) + 1)}
        if f == StrategyFamily.RSI_REVERSION:
            n = int(p["lookback"])
            return {"gain": RollingMean(n), "loss": RollingMean(n), "count": 0}
        raise ValueError(f"{f} is not a runtime strategy family")

    def warm(self, sym: str, ts: np.ndarray, close: np.ndarray, high: np.ndarray, low: np.ndarray) -> None:
        """Initialize from history with the vectorized family function (the research definition)."""
        st = self.state[sym]
        st.ind = self._indicators()
        if len(close) == 0 or self.v.family == StrategyFamily.BUY_HOLD:
            st.weight_bps = 10_000 if self.v.family == StrategyFamily.BUY_HOLD else 0
            return
        w = signal(self.v.family, self.p, Series(close, high, low))
        st.weight_bps = int(w[-1])
        st.held = st.weight_bps > 0
        if self.v.family == StrategyFamily.VOL_TARGET:
            r = np.zeros(len(close))
            r[1:] = np.diff(np.log(close))
            for x in r[-st.ind["vol"].n:]:
                st.ind["vol"].push(float(x))
            for x in close[-st.ind["trend"].n:]:
                st.ind["trend"].push(float(x))
        elif self.v.family == StrategyFamily.BREAKOUT:
            st.ind["high"].extend(float(x) for x in high[-st.ind["high"].maxlen:])
            st.ind["low"].extend(float(x) for x in low[-st.ind["low"].maxlen:])
        elif self.v.family == StrategyFamily.TS_MOMENTUM:
            st.ind["close"].extend(float(x) for x in close[-st.ind["close"].maxlen:])
        elif self.v.family == StrategyFamily.RSI_REVERSION:
            d = np.r_[0.0, np.diff(close)]
            for x in d[-int(self.p["lookback"]):]:
                st.ind["gain"].push(float(max(x, 0)))
                st.ind["loss"].push(float(max(-x, 0)))
            st.ind["count"] = len(close)
        else:
            for ind in st.ind.values():
                for x in close[-ind.n:]:
                    ind.push(float(x))
        st.last_ts = int(ts[-1])
        st.last_close = float(close[-1])
        self._describe(st, float(close[-1]))

    def _update(self, st: _SymbolState, c: float, high: float | None = None, low: float | None = None) -> None:
        f, p, ind = self.v.family, self.p, st.ind
        if f == StrategyFamily.SMA_CROSSOVER:
            ind["fast"].push(c)
            ind["slow"].push(c)
            st.weight_bps = 10_000 if ind["fast"].ready and ind["slow"].ready and ind["fast"].value > ind["slow"].value else 0
        elif f == StrategyFamily.ZSCORE_REVERSION:
            ind["mean"].push(c)
            ind["std"].push(c)
            if ind["mean"].ready:
                sd = ind["std"].value
                z = (c - ind["mean"].value) / sd if sd > 0 else 0.0
                if not st.held and z < -p["entry_z"]:
                    st.held = True
                elif st.held and z > p["exit_z"]:
                    st.held = False
            st.weight_bps = 10_000 if st.held else 0
        elif f == StrategyFamily.BREAKOUT:
            if len(ind["high"]) == ind["high"].maxlen:
                if not st.held and c > max(ind["high"]):
                    st.held = True
                elif st.held and c < min(ind["low"]):
                    st.held = False
            ind["high"].append(c if high is None else high)
            ind["low"].append(c if low is None else low)
            st.weight_bps = 10_000 if st.held else 0
        elif f == StrategyFamily.TS_MOMENTUM:
            ind["close"].append(c)
            st.score = c / ind["close"][0] - 1 if len(ind["close"]) == ind["close"].maxlen else 0.0
            st.weight_bps = 10_000 if len(ind["close"]) == ind["close"].maxlen and st.score > p["threshold"] else 0
        elif f == StrategyFamily.RSI_REVERSION:
            d = c - st.last_close if st.last_close > 0 else 0.0
            ind["gain"].push(max(d, 0.0))
            ind["loss"].push(max(-d, 0.0))
            ind["count"] += 1
            rsi = 100 - 100 / (1 + ind["gain"].value / ind["loss"].value) if ind["loss"].value > 0 else 100.0
            if ind["count"] > int(p["lookback"]):
                if not st.held and rsi < p["entry_rsi"]:
                    st.held = True
                elif st.held and rsi > p["exit_rsi"]:
                    st.held = False
            st.score = rsi
            st.weight_bps = 10_000 if st.held else 0
        else:
            ind["vol"].push(math.log(c / st.last_close) if st.last_close > 0 else 0.0)
            ind["trend"].push(c)
            vol = ind["vol"].value * math.sqrt(BARS_PER_YEAR) if ind["vol"].ready else float("nan")
            above = ind["trend"].ready and c > ind["trend"].value
            st.weight_bps = _bps(min(1.0, p["target_vol"] / vol)) if above and vol > 0 else 0
        st.last_close = c
        self._describe(st, c)

    def _describe(self, st: _SymbolState, c: float) -> None:
        f, ind = self.v.family, st.ind
        if f == StrategyFamily.SMA_CROSSOVER and ind["slow"].ready:
            fa, sl = ind["fast"].value, ind["slow"].value
            st.score, st.features = (fa - sl) / sl, {"sma_fast": fa, "sma_slow": sl}
            st.reasons = ["FAST_ABOVE_SLOW" if fa > sl else "FAST_BELOW_SLOW"]
        elif f == StrategyFamily.ZSCORE_REVERSION and ind["mean"].ready:
            sd = ind["std"].value
            z = (c - ind["mean"].value) / sd if sd > 0 else 0.0
            st.score, st.features = -z, {"zscore": z, "mean": ind["mean"].value, "std": sd}
            st.reasons = ["HOLDING_AFTER_ENTRY" if st.held else ("BELOW_ENTRY" if z < -self.p["entry_z"] else "FLAT")]
        elif f == StrategyFamily.VOL_TARGET and ind["vol"].ready and ind["trend"].ready:
            vol = ind["vol"].value * math.sqrt(BARS_PER_YEAR)
            st.score = min(1.0, self.p["target_vol"] / vol) if vol > 0 else 0.0
            st.features = {"realized_vol": vol, "trend": ind["trend"].value}
            st.reasons = ["ABOVE_TREND" if c > ind["trend"].value else "BELOW_TREND"]
        elif f in (StrategyFamily.BREAKOUT, StrategyFamily.RSI_REVERSION, StrategyFamily.TS_MOMENTUM):
            st.features = {"target_bps": float(st.weight_bps)}
            st.reasons = ["SIGNAL_LONG" if st.weight_bps else "SIGNAL_FLAT"]
        else:
            st.score, st.features, st.reasons = 0.0, {}, ["WARMING_UP"]

    # ------------------------------------------------------------------ decisions

    def _basket_on_snapshot(self, snap: MarketSnapshot) -> None:
        views = {s: snap.instruments.get(s) for s in self.v.universe}
        if any(v is None for v in views.values()):
            return
        first = self.v.universe[0]
        st0 = self.state[first]
        ts = views[first]._ts
        k = bisect.bisect_right(ts, st0.last_ts)
        for i in range(k, len(ts)):
            t = ts[i]
            closes = {}
            for s, v in views.items():
                j = bisect.bisect_right(v._ts, t) - 1  # forward-fill: last bar at or before t
                if j >= 0:
                    closes[s] = float(v._close[j])
            w = self._basket_update(closes)
            for s in self.v.universe:
                st = self.state[s]
                st.weight_bps, st.last_ts = w, int(t)
                st.score = self._feat["basket_z"]
                st.features = dict(self._feat)
                st.reasons = ["HOLD" if w else "STEPPED_ASIDE_RANGE_BOUND_AND_STRETCHED"]

    def holding_bars(self) -> int:
        p = self.p
        return int(p.get("slow") or p.get("lookback") or p.get("vol_lookback") or 1)

    def on_snapshot(self, snap: MarketSnapshot, interval_s: int = 3600) -> list[Intent]:
        if self.allocation:
            self.allocator.update(snap)
            a = self.allocator
            return [Intent(
                intent_id=f"{self.v.strategy_id}.{self.v.version}.{sym}.{snap.ts}", strategy_id=self.v.strategy_id,
                strategy_version=self.v.version, instrument=sym, venue=self.venue, md_seq=snap.gateway_seq,
                decision_ts=snap.ts, target_weight_bps=a.weights[sym], weight_scope="sleeve",
                allocation_id=a.allocation_id, score=round(a.scores[sym], 8),
                score_meaning=self.v.card.score_meaning[:200], holding_bars=max(1, int(self.p["rebalance_sessions"]) * 7),
                max_slippage_bps=self.v.card.max_slippage_bps, expires_ts=snap.ts + interval_s,
                features={"lookback_sessions": self.p["lookback_sessions"]},
                reasons=["FROZEN_SESSION_ALLOCATION", f"SIGNAL_THROUGH_{a.latest_signal_date or 'WARMUP'}"])
                    for sym in self.v.universe if sym in snap.instruments]
        out = []
        if self.basket:
            self._basket_on_snapshot(snap)
        for sym in self.v.universe:
            view: InstrumentView | None = snap.instruments.get(sym)
            st = self.state[sym]
            if view is None:
                continue
            if not st.ind:
                st.ind = self._indicators()
            if self.v.family == StrategyFamily.BUY_HOLD:
                st.weight_bps, st.reasons, st.last_ts = 10_000, ["PASSIVE_CORE"], view.last_ts
            elif not self.basket:
                ts = view._ts  # read-only tuple; values are copied below
                k = bisect.bisect_right(ts, st.last_ts)
                for i in range(k, len(ts)):
                    self._update(st, float(view._close[i]), float(view._high[i]), float(view._low[i]))
                    st.last_ts = int(ts[i])
            out.append(Intent(
                intent_id=f"{self.v.strategy_id}.{self.v.version}.{sym}.{snap.ts}",
                strategy_id=self.v.strategy_id, strategy_version=self.v.version, instrument=sym, venue=self.venue,
                md_seq=snap.gateway_seq, decision_ts=snap.ts, target_weight_bps=st.weight_bps,
                score=round(float(st.score), 8), score_meaning=self.v.card.score_meaning[:200],
                holding_bars=self.holding_bars(), max_slippage_bps=self.v.card.max_slippage_bps,
                expires_ts=snap.ts + interval_s, features={k: round(float(v), 8) for k, v in st.features.items()},
                reasons=list(st.reasons)))
        return out
