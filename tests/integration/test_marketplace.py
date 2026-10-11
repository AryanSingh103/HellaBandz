"""Real Postgres transactions: license and credit conservation under contention."""
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from hellabandz.contracts import MICROS

pytestmark = pytest.mark.db
ROOT = "/v1/marketplace"
ASSET = "nike-review-radar-v1"
SEED = "L" + ASSET


def start(stack):
    stack.start_api(OPENAI_API_KEY="", ANTHROPIC_API_KEY="")
    return httpx.Client(base_url=stack.base, timeout=30)


def buy(client, key="buy", quantity=1, listing=SEED, account="demo-buyer"):
    return client.post(ROOT + "/buy", json={"request_id": key, "quantity": quantity, "listing_id": listing, "account_id": account})


def sell(client, key="sell", quantity=1, price="125.50", account="demo-buyer"):
    return client.post(ROOT + "/listings", json={"request_id": key, "quantity": quantity, "asset_id": ASSET,
                                                "price_credits": price, "account_id": account})


def conserved(stack):
    assert stack.sql("SELECT sum(balance) = sum(credit_grant), min(balance) >= 0 FROM strategy_accounts")[0] == (True, True)
    for asset, supply in stack.sql("SELECT asset_id, supply FROM strategy_assets"):
        held = stack.sql("SELECT coalesce(sum(quantity),0) FROM strategy_holdings WHERE asset_id=%s", (asset,))[0][0]
        escrow = stack.sql("SELECT coalesce(sum(remaining),0) FROM strategy_listings WHERE asset_id=%s", (asset,))[0][0]
        assert held + escrow == supply
    assert stack.sql("SELECT count(*) FROM commands")[0][0] == 0  # separate from execution capital


def test_license_purchase_resale_cancel_and_idempotency(stack):
    with start(stack) as c:
        assert buy(c, quantity=3).status_code == 200
        assert buy(c, quantity=3).json()["duplicate"] is True
        assert buy(c, quantity=4).status_code == 409
        wallet = c.get(ROOT + "/wallet", params={"account_id": "demo-buyer"}).json()
        assert wallet["balance"] == 9700 * MICROS and wallet["holdings"][0]["quantity"] == 3
        listing = sell(c, quantity=2).json()
        assert sell(c, quantity=2).json()["listing_id"] == listing["listing_id"]
        assert sell(c, quantity=3).status_code == 409  # conflicting idempotency payload
        assert sell(c, key="oversell", quantity=2).status_code == 409
        assert buy(c, key="self", listing=listing["listing_id"]).status_code == 409
        assert buy(c, key="secondary", listing=listing["listing_id"], account="demo-seller").status_code == 200
        path = ROOT + f"/listings/{listing['listing_id']}/cancel"
        assert c.post(path, json={"account_id": "demo-seller"}).status_code == 404
        assert c.post(path, json={"account_id": "demo-buyer"}).status_code == 200
        assert c.post(path, json={"account_id": "demo-buyer"}).status_code == 200
        assert buy(c, key="cancelled", listing=listing["listing_id"], account="demo-seller").status_code == 409
        before = c.get(ROOT + "/wallet", params={"account_id": "demo-buyer"}).json()
        assert before["balance"] == 9825500000 and before["holdings"][0]["quantity"] == 2
        c.post(ROOT + "/sync")
        assert c.get(ROOT + "/wallet", params={"account_id": "demo-buyer"}).json() == before
        assert stack.sql("SELECT remaining FROM strategy_listings WHERE listing_id=%s", (SEED,))[0][0] == 97
        conserved(stack)


def test_concurrent_buyers_and_retries_cannot_duplicate_or_overdraw(stack):
    with start(stack) as c, ThreadPoolExecutor(max_workers=12) as pool:
        # More buyers than the 100-unit inventory; only one 60-unit purchase can win.
        outcomes = list(pool.map(lambda i: buy(c, key=f"contend-{i}", quantity=60,
                                              account="demo-buyer" if i % 2 else "demo-seller").status_code, range(12)))
        assert outcomes.count(200) == 1 and outcomes.count(409) == 11
        assert stack.sql("SELECT sum(quantity) FROM strategy_trades")[0][0] == 60
        results = list(pool.map(lambda _: buy(c, key="retry-same", quantity=1).status_code, range(12)))
        assert results == [200] * 12
        assert stack.sql("SELECT count(*) FROM strategy_trades WHERE request_id='retry-same'")[0][0] == 1
        # Make a pricey secondary offer that neither identity can afford.
        owner = stack.sql("SELECT buyer_id FROM strategy_trades WHERE quantity=60")[0][0]
        listing = sell(c, key="costly", price="100000", account=owner).json()
        other = "demo-seller" if owner == "demo-buyer" else "demo-buyer"
        assert buy(c, key="poor", listing=listing["listing_id"], account=other).status_code == 409
        conserved(stack)


def test_concurrent_oversell_and_cancel_vs_purchase_conserve_licenses(stack):
    with start(stack) as c, ThreadPoolExecutor(max_workers=8) as pool:
        assert buy(c, quantity=5).status_code == 200
        results = list(pool.map(lambda i: sell(c, key=f"sell-{i}", quantity=4), range(8)))
        assert sum(r.status_code == 201 for r in results) == 1
        listing = next(r.json() for r in results if r.status_code == 201)
        def race(i):
            if i % 2:
                return c.post(ROOT + f"/listings/{listing['listing_id']}/cancel", json={"account_id": "demo-buyer"})
            return buy(c, key=f"race-{i}", listing=listing["listing_id"], account="demo-seller")
        outcomes = list(pool.map(race, range(8)))
        assert all(r.status_code in (200, 409) for r in outcomes)
        conserved(stack)


def test_marketplace_rejects_invalid_inputs_and_saves_review_evidence(stack):
    with start(stack) as c:
        for quantity in [0, -1, True, 1.5, 10001]:
            assert buy(c, key="invalid", quantity=quantity).status_code == 422
        for price in ["0", "-1", "NaN", "Infinity", "0.001", "100001"]:
            assert sell(c, price=price).status_code == 422
        data = c.get(ROOT + "/reviews/example").json()
        payload = {k: data[k] for k in ("as_of", "provider", "reviews")}
        r = c.post(ROOT + "/reviews/analyze", json=payload)
        assert r.status_code == 200 and r.json()["signal"] == "avoid_or_reduce"
        assert c.post(ROOT + "/reviews/analyze", json=payload).json()["digest"] == r.json()["digest"]
        assert len(c.get(ROOT + "/reviews/history").json()) == 1
        assert c.post(ROOT + "/reviews/analyze", json={**payload, "provider": "claude"}).status_code == 422
        conserved(stack)
