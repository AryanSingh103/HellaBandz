"""Verified research receipts must survive API publication and reject mismatches."""
from fastapi.testclient import TestClient
from solders.signature import Signature

from quantswarm.api import app as gateway
from quantswarm.audit import solana
from quantswarm.ledger import canonical, sha256_hex


def test_solana_receipt_persistence_verification_and_mismatch(monkeypatch):
    manifest = {"run_id": "T-receipt", "frozen": True}
    h = sha256_hex(canonical(manifest))
    sig = str(Signature.default())
    record = {"manifest": manifest, "manifest_hash": h, "solana_sig": None}

    async def query(sql, args=()):
        if sql.startswith("UPDATE tournaments"):
            record["solana_sig"] = args[0]
        return [record.copy()]

    monkeypatch.setattr(gateway, "q", query)
    monkeypatch.setattr(solana, "onchain_memo", lambda signature: solana.memo_for("T-receipt", h))
    c = TestClient(gateway.app)
    route = "/v1/tournaments/T-receipt/solana"
    assert c.get(route).json()["status"] == "not_committed"
    body = {"run_id": "T-receipt", "manifest": manifest, "manifest_hash": h, "solana_sig": sig}
    assert c.post("/v1/tournaments", json=body).status_code == 200
    assert c.get("/v1/tournaments").json()[0]["solana_sig"] == sig
    assert c.get(route).json()["status"] == "verified"
    # A signature-free immutable retry cannot erase the proof.
    assert c.post("/v1/tournaments", json={**body, "solana_sig": None}).status_code == 200
    assert record["solana_sig"] == sig
    monkeypatch.setattr(solana, "onchain_memo", lambda signature: "wrong manifest")
    assert c.post("/v1/tournaments", json=body).status_code == 422
    assert c.get(route).json()["status"] == "mismatch"
    assert not c.get(route).json()["ok"]
    assert c.post("/v1/tournaments", json={**body, "solana_sig": "invalid"}).status_code == 422
    assert record["solana_sig"] == sig


def test_failed_transaction_memo_cannot_verify(monkeypatch):
    monkeypatch.setattr(solana, "rpc", lambda *args: {"meta": {"err": {"InstructionError": [0, "failed"]}},
        "transaction": {"message": {"instructions": [{"programId": solana.MEMO_PROGRAM, "parsed": "matching"}]}}})
    assert solana.onchain_memo("signature") is None
