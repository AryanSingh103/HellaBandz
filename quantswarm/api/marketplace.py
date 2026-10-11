"""A local, virtual-credit strategy-license exchange, separate from execution cash.

Listings escrow licenses. Atomic purchases transfer credits and licenses together.
Demo identities are selectable, not authenticated users; no real payments exist.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from quantswarm.api import app as gateway
from quantswarm.contracts import Ident, MICROS, usd_to_micros
from quantswarm.agents.reviews import ProviderConfigurationError, ReviewRequest, analyze, example

router = APIRouter(prefix="/v1/marketplace")
review_slots = asyncio.Semaphore(2)
NAMES = {"sma_crossover": "Moving Average Crossover", "breakout": "Price Breakout",
         "zscore_reversion": "Z-Score Reversion", "vol_target": "Volatility Target"}
NIKE = {"asset_id": "nike-review-radar-v1", "title": "Nike Review Radar", "family": "review_sentiment",
        "agent_id": "consumer_research", "universe": ["NKE"], "status": "research",
        "description": "Analyze dated customer reviews for weakening product quality, value and demand. "
                       "Claude-ready; provider is labeled on every analysis. A bearish signal means avoid or reduce exposure.",
        "metrics": {"train": None, "validation": None, "test": None}, "run_id": None,
        "dataset_id": None, "periods": None, "source": "Review research; no price backtest",
        "gates": [], "params": {"recent_days": 30, "baseline_days": 90, "minimum_reviews_per_window": 5},
        "note": "The alleged Claude prediction of Nike's crash could not be independently verified. "
                "Supplied sources discuss social chatter and shopping agents, not a documented Claude forecast. "
                "This experimental strategy has no measured Sharpe, returns or drawdowns yet."}


async def sync_catalog():
    """Import frozen research once; never replenish sold inventory on refresh."""
    assets = [NIKE]
    for row in await gateway.q("SELECT manifest FROM tournaments ORDER BY created_at DESC LIMIT 100"):
        m = row["manifest"]
        for strategy in m.get("ranked", []):
            spec = next((f.get("spec") for f in m.get("fitted", [])
                         if f.get("spec", {}).get("strategy_id") == strategy.get("strategy_id")), None)
            if not spec:
                continue
            proposal = next((p for p in m.get("proposals", []) if p.get("agent_id") == strategy["agent_id"]), {})
            assets.append({"asset_id": "S" + hashlib.sha256(strategy["strategy_id"].encode()).hexdigest()[:24],
                           "strategy_id": strategy["strategy_id"], "title": NAMES.get(strategy["family"], strategy["family"]),
                           "family": strategy["family"], "agent_id": strategy["agent_id"], "status": "evaluated",
                           "universe": spec["universe"], "params": spec["params"], "run_id": m["run_id"],
                           "dataset_id": m["dataset"]["dataset_id"], "periods": m["dataset"].get("splits"),
                           "dataset_hash": m["dataset"]["content_sha256"], "source": m["dataset"].get("source"),
                           "quote_currency": m["dataset"].get("quote_currency", "USD"),
                           "sharpe_bars_per_year": 252 * 7,
                           "description": proposal.get("proposal", {}).get("rationale", "Recorded strategy research"),
                           "provider": proposal.get("source", "unavailable"), "gates": strategy.get("gates", []),
                           "metrics": {"train": strategy.get("train"), "validation": strategy.get("validation"),
                                       "test": m.get("held_out_test", {}).get("per_strategy_standalone", {}).get(strategy["strategy_id"])},
                           "note": "Metrics are historical strategy backtests, not returns from buying a license."})
    async with gateway.pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(424244)")
        for account, grant in [("research-desk", 0), ("demo-buyer", 10000 * MICROS), ("demo-seller", 10000 * MICROS)]:
            await conn.execute("INSERT INTO strategy_accounts VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (account, grant, grant))
        for asset in assets:
            added = await (await conn.execute("INSERT INTO strategy_assets VALUES (%s,%s,100) "
                                             "ON CONFLICT DO NOTHING RETURNING asset_id", (asset["asset_id"], Jsonb(asset)))).fetchone()
            if added:
                await conn.execute("INSERT INTO strategy_listings (listing_id,request_id,config,asset_id,seller_id,price,remaining) "
                                   "VALUES (%s,%s,'{}',%s,'research-desk',%s,100)",
                                   ("L" + asset["asset_id"], "seed:" + asset["asset_id"], asset["asset_id"], 100 * MICROS))
    return {"status": "synced", "assets": len(assets)}


@router.post("/sync")
async def publish_research():
    return await sync_catalog()


@router.get("")
async def catalog():
    return {"mode": "local virtual-credit demo", "assets": await gateway.q("SELECT descriptor FROM strategy_assets ORDER BY asset_id"),
            "listings": await gateway.q("SELECT * FROM strategy_listings WHERE remaining > 0 ORDER BY price, created_at"),
            "accounts": await gateway.q("SELECT account_id FROM strategy_accounts WHERE account_id <> 'research-desk' ORDER BY account_id")}


@router.get("/wallet")
async def wallet(account_id: Ident):
    accounts = await gateway.q("SELECT * FROM strategy_accounts WHERE account_id = %s", (account_id,))
    if not accounts:
        raise HTTPException(404, "demo account not found")
    return {**accounts[0], "holdings": await gateway.q("SELECT h.*, a.descriptor FROM strategy_holdings h "
                                                     "JOIN strategy_assets a USING(asset_id) WHERE account_id = %s AND quantity > 0", (account_id,)),
            "listings": await gateway.q("SELECT * FROM strategy_listings WHERE seller_id = %s AND remaining > 0", (account_id,)),
            "trades": await gateway.q("SELECT * FROM strategy_trades WHERE buyer_id = %s OR seller_id = %s "
                                      "ORDER BY created_at DESC LIMIT 100", (account_id, account_id))}


class Purchase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Ident
    account_id: Ident
    listing_id: Ident
    quantity: int = Field(ge=1, le=10000, strict=True)


class Listing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Ident
    account_id: Ident
    asset_id: Ident
    quantity: int = Field(ge=1, le=10000, strict=True)
    price_credits: Decimal = Field(ge=Decimal("0.01"), le=100000, decimal_places=2)


async def take_request_lock(conn, operation, request_id):
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (operation + ":" + request_id,))


@router.post("/buy")
async def buy(body: Purchase):
    config = body.model_dump(mode="json")
    async with gateway.pool.connection() as conn, conn.transaction():
        await take_request_lock(conn, "market-buy", body.request_id)
        previous = await (await conn.execute("SELECT * FROM strategy_trades WHERE request_id = %s", (body.request_id,))).fetchone()
        if previous:
            if previous["config"] != config:
                raise HTTPException(409, "request ID reused with different purchase")
            return {**previous, "duplicate": True}
        listing = await (await conn.execute("SELECT * FROM strategy_listings WHERE listing_id = %s FOR UPDATE", (body.listing_id,))).fetchone()
        if not listing or listing["remaining"] < body.quantity:
            raise HTTPException(409, "listing has insufficient available licenses")
        if listing["seller_id"] == body.account_id:
            raise HTTPException(409, "cannot purchase your own listing")
        accounts = await (await conn.execute("SELECT * FROM strategy_accounts WHERE account_id IN (%s,%s) "
                                            "ORDER BY account_id FOR UPDATE", (body.account_id, listing["seller_id"]))).fetchall()
        buyer = next((a for a in accounts if a["account_id"] == body.account_id), None)
        total = listing["price"] * body.quantity
        if not buyer or buyer["balance"] < total:
            raise HTTPException(409, "insufficient virtual credits")
        await conn.execute("UPDATE strategy_accounts SET balance = balance - %s WHERE account_id = %s", (total, body.account_id))
        await conn.execute("UPDATE strategy_accounts SET balance = balance + %s WHERE account_id = %s", (total, listing["seller_id"]))
        await conn.execute("UPDATE strategy_listings SET remaining = remaining - %s WHERE listing_id = %s", (body.quantity, body.listing_id))
        await conn.execute("INSERT INTO strategy_holdings VALUES (%s,%s,%s) ON CONFLICT (account_id,asset_id) "
                           "DO UPDATE SET quantity = strategy_holdings.quantity + excluded.quantity",
                           (body.account_id, listing["asset_id"], body.quantity))
        trade = await (await conn.execute("INSERT INTO strategy_trades (request_id,config,listing_id,asset_id,buyer_id,seller_id,quantity,price,total) "
                                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                                         (body.request_id, Jsonb(config), body.listing_id, listing["asset_id"], body.account_id,
                                          listing["seller_id"], body.quantity, listing["price"], total))).fetchone()
    return {**trade, "duplicate": False}


@router.post("/listings", status_code=201)
async def sell(body: Listing):
    config = body.model_dump(mode="json")
    async with gateway.pool.connection() as conn, conn.transaction():
        await take_request_lock(conn, "market-sell", body.request_id)
        previous = await (await conn.execute("SELECT * FROM strategy_listings WHERE request_id = %s", (body.request_id,))).fetchone()
        if previous:
            if previous["config"] != config:
                raise HTTPException(409, "request ID reused with different listing")
            return previous
        await conn.execute("SELECT account_id FROM strategy_accounts WHERE account_id = %s FOR UPDATE", (body.account_id,))
        reserved = await (await conn.execute("UPDATE strategy_holdings SET quantity = quantity - %s "
                                            "WHERE account_id = %s AND asset_id = %s AND quantity >= %s RETURNING quantity",
                                            (body.quantity, body.account_id, body.asset_id, body.quantity))).fetchone()
        if not reserved:
            raise HTTPException(409, "you do not own enough available licenses")
        listing = await (await conn.execute("INSERT INTO strategy_listings (listing_id,request_id,config,asset_id,seller_id,price,remaining) "
                                           "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                                           ("L" + uuid.uuid4().hex, body.request_id, Jsonb(config), body.asset_id,
                                            body.account_id, usd_to_micros(body.price_credits), body.quantity))).fetchone()
    return listing


class Cancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: Ident


@router.post("/listings/{listing_id}/cancel")
async def cancel(listing_id: Ident, body: Cancel):
    async with gateway.pool.connection() as conn, conn.transaction():
        listing = await (await conn.execute("SELECT * FROM strategy_listings WHERE listing_id = %s FOR UPDATE", (listing_id,))).fetchone()
        if not listing or listing["seller_id"] != body.account_id:
            raise HTTPException(404, "listing not owned by this account")
        await conn.execute("SELECT account_id FROM strategy_accounts WHERE account_id = %s FOR UPDATE", (body.account_id,))
        if listing["remaining"]:
            await conn.execute("INSERT INTO strategy_holdings VALUES (%s,%s,%s) ON CONFLICT (account_id,asset_id) "
                               "DO UPDATE SET quantity = strategy_holdings.quantity + excluded.quantity",
                               (body.account_id, listing["asset_id"], listing["remaining"]))
            await conn.execute("UPDATE strategy_listings SET remaining = 0 WHERE listing_id = %s", (listing_id,))
    return {"status": "cancelled", "listing_id": listing_id}


@router.get("/reviews/example")
async def review_example():
    return example()


@router.post("/reviews/analyze")
async def review_analysis(body: ReviewRequest):
    async with review_slots:
        try:
            result = await asyncio.to_thread(analyze, body)
        except ProviderConfigurationError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "Provider returned invalid review classifications; analysis rejected.") from exc
        except Exception as exc:
            # Upstream exceptions can contain authentication headers or request text.
            raise HTTPException(502, "Review provider failed; retry or choose the offline rating baseline.") from exc
    async with gateway.pool.connection() as conn:
        await conn.execute("INSERT INTO review_analyses (digest,result) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                           (result["digest"], Jsonb(result)))
    return result


@router.get("/reviews/history")
async def review_history():
    return await gateway.q("SELECT digest, created_at, result FROM review_analyses ORDER BY created_at DESC LIMIT 20")
