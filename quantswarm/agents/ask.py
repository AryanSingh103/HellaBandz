"""Grounded Q&A over live run data (used by the voice/terminal "Ask" panel).

Facts are assembled deterministically from database rows. If an OpenAI key is
configured the model only rephrases those facts (it is told to refuse anything
not in them); otherwise a deterministic answer is returned. Read-only: this
path cannot place or change orders.
"""

from __future__ import annotations

import json

from quantswarm.agents import llm

RULE_TEXT = {
    "R0_PORTFOLIO_NOT_OPEN": "the portfolio was not open yet",
    "R1_MAX_ORDER_VALUE": "a single order may not exceed 25% of portfolio equity",
    "R2_AGENT_ALLOCATION": "the agent would exceed the capital the allocator gave it",
    "R2_ALLOCATIONS_EXCEED_EQUITY": "total allocations would exceed equity",
    "R3_GROSS_EXPOSURE": "positions plus open buys may not exceed 90% of equity",
    "R4_BUYING_POWER": "not enough unreserved cash",
    "R5_POSITION": "the sell was larger than the agent's unreserved position",
    "R5_NO_POSITION": "the agent holds no unreserved shares to sell (long-only, no shorting)",
    "R6_INVALID_QTY": "quantity must be a positive whole number",
    "R6_INVALID_LIMIT": "the limit price was missing or invalid",
    "R6_LIMIT_OUTSIDE_BAND": "the limit price was more than 10% away from the last close",
    "R6_UNKNOWN_SYMBOL": "no market data exists for the symbol",
    "R7_STALE_SIGNAL": "the decision was based on market data more than two hours old",
    "R7_FUTURE_SIGNAL": "the order referenced a bar that has not happened yet",
    "R8_DUPLICATE_ID_DIFFERENT_PAYLOAD": "the order id was reused with different contents",
    "R9_KILL_SWITCH": "the kill switch is on, so new buys are blocked",
}


def facts(ctx: dict) -> dict:
    out: dict = {}
    p = ctx.get("portfolio")
    if p:
        st = p["state"]
        pos = {k: v["qty"] for k, v in st.get("positions", {}).items() if v.get("qty")}
        out["portfolio"] = {
            "equity_usd": round(p["equity"] / 1e6, 2), "cash_usd": round(st["cash"] / 1e6, 2),
            "market_value_usd": round(st.get("market_value", 0) / 1e6, 2),
            "exposure_pct": round(100 * st.get("market_value", 0) / max(1, p["equity"]), 1),
            "positions": pos, "open_orders": len(st.get("orders", {})), "kill_switch": st.get("kill_switch"),
            "kill_reason": st.get("kill_reason"), "realized_pnl_usd": round(st.get("realized_pnl", 0) / 1e6, 2),
            "fees_usd": round(st.get("fees_paid", 0) / 1e6, 2), "ledger_digest": p["digest"][:16],
        }
    flagged = [o for o in ctx.get("recent_orders", []) if o["outcome"] in ("REJECT", "REDUCE")]
    out["recent_rejected_or_reduced"] = [
        {"order": o["order_id"], "agent": o["agent_id"], "symbol": o["symbol"], "side": o["side"],
         "requested": o["requested_qty"], "approved": o["approved_qty"], "outcome": o["outcome"],
         "why": [RULE_TEXT.get(r, r) for r in o["rules"]]} for o in flagged[:8]]
    out["rejection_counts"] = {r["rule"]: r["n"] for r in ctx.get("rejections", [])}
    t = (ctx.get("tournament") or {}).get("manifest")
    if t:
        out["tournament"] = {
            "run_id": t["run_id"],
            "strategies": [{"id": r["strategy_id"], "family": r["family"], "train_sharpe": r["train"]["sharpe"],
                            "validation_sharpe": r["validation"]["sharpe"], "score": r["score"], "gates": r["gates"],
                            "weight": t["allocations"]["by_strategy"][r["strategy_id"]]["weight"]}
                           for r in t["ranked"]],
            "held_out_return": t["held_out_test"]["portfolio"]["metrics"]["net_return"],
        }
    return out


def deterministic_answer(q: str, f: dict) -> str:
    ql = q.lower()
    parts: list[str] = []
    if any(w in ql for w in ("reject", "why", "denied", "blocked", "reduce")):
        rr = f.get("recent_rejected_or_reduced") or []
        if not rr:
            parts.append("No orders have been rejected or reduced recently.")
        for o in rr[:3]:
            verdict = "rejected" if o["outcome"] == "REJECT" else f"reduced to {o['approved']}"
            parts.append(f"{o['agent']}'s {o['side']} of {o['requested']} {o['symbol']} was {verdict} because "
                         + " and ".join(o["why"]) + ".")
    if any(w in ql for w in ("best", "perform", "win", "tournament", "agent", "strategy")) and f.get("tournament"):
        t = f["tournament"]
        best = max(t["strategies"], key=lambda s: s["score"])
        parts.append(f"In tournament {t['run_id']} the best validated strategy was {best['id']} "
                     f"({best['family']}, validation Sharpe {best['validation_sharpe']:.2f}, weight {best['weight']:.0%}).")
        for s in t["strategies"]:
            if s["score"] == 0:
                parts.append(f"{s['id']} got no capital: train Sharpe {s['train_sharpe']:.2f} but "
                             f"{'; '.join(s['gates'])} on validation.")
        parts.append(f"The frozen allocation returned {t['held_out_return']:.1%} on the held-out period.")
    if any(w in ql for w in ("exposure", "position", "cash", "equity", "hold", "portfolio", "kill")) and f.get("portfolio"):
        p = f["portfolio"]
        parts.append(f"Equity is ${p['equity_usd']:,.2f} with ${p['cash_usd']:,.2f} cash; exposure is {p['exposure_pct']}% "
                     f"across {len(p['positions'])} positions. Kill switch is {'ON (' + str(p['kill_reason']) + ')' if p['kill_switch'] else 'off'}.")
    if not parts:
        p = f.get("portfolio")
        parts.append("I can answer questions about rejected orders, agent performance, and current exposure."
                     + (f" Right now equity is ${p['equity_usd']:,.2f}." if p else ""))
    return " ".join(parts)


SYSTEM = """You answer questions about a simulated trading system. Use ONLY the JSON facts provided.
If the facts do not contain the answer, say you don't know. Be concise (<= 4 sentences), cite numbers
exactly, and never give investment advice. Everything is paper trading."""


def answer(question: str, ctx: dict) -> dict:
    f = facts(ctx)
    base = deterministic_answer(question, f)
    if llm.available():
        try:
            raw, meta = llm.chat_json(SYSTEM + ' Respond as JSON {"answer": str}.',
                                      json.dumps({"question": question, "facts": f}), temperature=0.2, timeout=20)
            return {"answer": str(raw.get("answer", base)), "mode": "llm", "llm": meta, "facts": f}
        except Exception as e:
            return {"answer": base, "mode": f"deterministic (llm error: {type(e).__name__})", "facts": f}
    return {"answer": base, "mode": "deterministic", "facts": f}
