"""Strategy attribution beneath one aggregate portfolio.

The ledger holds one execution account per portfolio. This book splits that account between
strategies ("sleeves") without creating or destroying anything. Rules, decided before the code:

1. Capital. Each strategy starts with the cash its research allocation gave it. Cash nobody was
   allocated stays "unallocated". Sum of sleeve capital + unallocated = the account's opening cash.
2. Fills. Every order carries an attribution plan (strategy -> planned lots) written by the
   coordinator when it decided the order. A fill of q lots is split across the strategies with
   lots still planned, in proportion to what each still has planned. Integer lots are assigned by
   largest remainder; ties go to the alphabetically first strategy id. The split always adds up to q.
3. Fees and slippage of a fill are split in the same proportion, also by largest remainder, so they
   add up to the ledger's numbers exactly.
4. Buys add lots and cost basis (price x lots, fees excluded, like the ledger) and remove cash.
   Sells remove lots and basis at the strategy's own average cost and realize the difference.
5. Internal transfers. When one strategy wants to sell and another wants to buy the same
   instrument in the same cycle, the coordinator moves lots between their sleeves at the last close
   instead of sending two orders. Cash moves the other way. Nothing changes in the account.
6. What adds up exactly, after every event: lots per instrument (sum of sleeves = account position),
   cash (sum of sleeves + unallocated = account cash), fees, slippage, and total profit (cash plus
   lots at market). What does not have to match: the split between realized and unrealized profit,
   because each sleeve uses its own average cost while the ledger pools the account. The total is the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def split(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Split an integer total in proportion to integer weights: largest remainder, ties by key."""
    w = {k: v for k, v in weights.items() if v > 0}
    if total == 0 or not w:
        return {}
    denom = sum(w.values())
    base = {k: total * v // denom for k, v in w.items()}
    rest = total - sum(base.values())
    order = sorted(w, key=lambda k: (-(total * w[k] % denom), k))
    for k in order[:rest]:
        base[k] += 1
    return {k: v for k, v in base.items() if v}


@dataclass
class Lot:
    qty: int = 0
    basis: int = 0


@dataclass
class Sleeve:
    capital: int
    cash: int
    fees: int = 0
    slippage: int = 0
    realized: int = 0
    traded_notional: int = 0
    fills: int = 0
    positions: dict[str, Lot] = field(default_factory=dict)

    def qty(self, sym: str) -> int:
        lot = self.positions.get(sym)
        return lot.qty if lot else 0


class AttributionError(AssertionError):
    pass


class AttributionBook:
    def __init__(self, opening_cash: int, capital: dict[str, int]):
        if sum(capital.values()) > opening_cash:
            raise ValueError("sleeve capital exceeds the account's opening cash")
        self.opening_cash = opening_cash
        self.sleeves = {k: Sleeve(capital=v, cash=v) for k, v in sorted(capital.items())}
        self.unallocated = opening_cash - sum(capital.values())
        self.plans: dict[str, dict[str, int]] = {}  # client_order_id -> strategy -> lots still planned
        self.applied_fills: set[str] = set()
        self.transfers = 0

    def qty(self, strategy: str, sym: str) -> int:
        return self.sleeves[strategy].qty(sym)

    def basis(self, strategy: str, sym: str) -> int:
        lot = self.sleeves[strategy].positions.get(sym)
        return lot.basis if lot else 0

    def total_qty(self, sym: str) -> int:
        return sum(s.qty(sym) for s in self.sleeves.values())

    def plan(self, order_id: str, attribution: dict[str, int]) -> None:
        self.plans[order_id] = {k: v for k, v in sorted(attribution.items()) if v > 0}

    def close_plan(self, order_id: str) -> None:
        self.plans.pop(order_id, None)

    def on_fill(self, fill_key: str, order_id: str, sym: str, side: str, qty: int, px: int, fee: int, slippage: int) -> dict[str, int]:
        """Attribute one ledger fill. Idempotent on fill_key. Returns strategy -> lots."""
        if fill_key in self.applied_fills:
            return {}
        plan = self.plans.get(order_id)
        if not plan or sum(plan.values()) < qty:
            raise AttributionError(f"fill {fill_key} for {order_id} has no attribution plan covering {qty} lots")
        shares = split(qty, plan)
        fees = split(fee, shares)
        slips = split(slippage, shares)
        for k, q in shares.items():
            sl = self.sleeves[k]
            lot = sl.positions.setdefault(sym, Lot())
            if side == "BUY":
                sl.cash -= q * px + fees.get(k, 0)
                lot.qty += q
                lot.basis += q * px
            else:
                if q > lot.qty:
                    raise AttributionError(f"{k} would sell {q} {sym} but holds {lot.qty}")
                removed = lot.basis * q // lot.qty
                lot.basis -= removed
                lot.qty -= q
                sl.realized += q * px - removed
                sl.cash += q * px - fees.get(k, 0)
            sl.fees += fees.get(k, 0)
            sl.slippage += slips.get(k, 0)
            sl.traded_notional += q * px
            sl.fills += 1
            plan[k] -= q
        self.plans[order_id] = {k: v for k, v in plan.items() if v > 0}
        self.applied_fills.add(fill_key)
        return shares

    def transfer(self, sym: str, src: str, dst: str, qty: int, px: int) -> None:
        a, b = self.sleeves[src], self.sleeves[dst]
        lot = a.positions.get(sym)
        if qty <= 0 or lot is None or lot.qty < qty:
            raise AttributionError(f"transfer of {qty} {sym} from {src} exceeds its {lot.qty if lot else 0}")
        removed = lot.basis * qty // lot.qty
        lot.basis -= removed
        lot.qty -= qty
        a.realized += qty * px - removed
        a.cash += qty * px
        dl = b.positions.setdefault(sym, Lot())
        dl.qty += qty
        dl.basis += qty * px
        b.cash -= qty * px
        self.transfers += 1

    # ------------------------------------------------------------------ reporting and checks

    def report(self, marks: dict[str, int]) -> dict[str, dict]:
        out = {}
        for k, sl in self.sleeves.items():
            mv = sum(lot.qty * marks.get(sym, 0) for sym, lot in sl.positions.items())
            basis = sum(lot.basis for lot in sl.positions.values())
            out[k] = {"capital": sl.capital, "cash": sl.cash, "market_value": mv, "equity": sl.cash + mv,
                      "pnl": sl.cash + mv - sl.capital, "realized": sl.realized, "unrealized": mv - basis,
                      "fees": sl.fees, "slippage": sl.slippage, "traded_notional": sl.traded_notional,
                      "fills": sl.fills,
                      "positions": {sym: {"qty": lot.qty, "basis": lot.basis} for sym, lot in sorted(sl.positions.items())
                                    if lot.qty}}
        return out

    def check(self, account_cash: int, account_qty: dict[str, int], account_fees: int, account_slippage: int,
              marks: dict[str, int]) -> None:
        def fail(msg):
            raise AttributionError(msg)

        syms = set(account_qty) | {s for sl in self.sleeves.values() for s in sl.positions}
        for sym in syms:
            if self.total_qty(sym) != account_qty.get(sym, 0):
                fail(f"{sym}: sleeves hold {self.total_qty(sym)}, account holds {account_qty.get(sym, 0)}")
        cash = sum(sl.cash for sl in self.sleeves.values()) + self.unallocated
        if cash != account_cash:
            fail(f"sleeve cash {cash} != account cash {account_cash}")
        if sum(sl.fees for sl in self.sleeves.values()) != account_fees:
            fail("sleeve fees do not add up to the account's fees")
        if sum(sl.slippage for sl in self.sleeves.values()) != account_slippage:
            fail("sleeve slippage does not add up to the account's slippage")
        for sl in self.sleeves.values():
            for sym, lot in sl.positions.items():
                if lot.qty < 0 or lot.basis < 0 or (lot.qty == 0 and lot.basis != 0):
                    fail(f"invalid sleeve lot {sym}: {lot}")
        total_equity = cash + sum(q * marks.get(s, 0) for s, q in account_qty.items())
        sleeve_equity = self.unallocated + sum(r["equity"] for r in self.report(marks).values())
        if total_equity != sleeve_equity:
            fail("sleeve equity does not add up to account equity")
