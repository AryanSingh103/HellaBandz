"""Audit reconstruction and tamper detection against disposable real Postgres."""
from copy import deepcopy

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from quantswarm.audit import traces, solana

PUBLIC = "A6fBZioMv4L9ob4jS7tCuhLgE3Z4bVECBpMTUToPLK4"


def snapshot(stack):
    with psycopg.connect(stack.url, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return traces.capture(conn)


def seed(stack, monkeypatch):
    monkeypatch.setenv("QS_AUDIT_SIGNER", PUBLIC)
    stack.sql("INSERT INTO strategy_accounts VALUES ('alice',100,100)")
    stack.sql("INSERT INTO agent_messages(run_id,agent_id,role,content) VALUES ('T1','a','research','{\"claim\":\"original\"}')")
    return traces.make_manifest(*snapshot(stack))


def test_trace_seal_reconstructs_snapshot_after_legitimate_activity(stack, monkeypatch):
    manifest = seed(stack, monkeypatch)
    stack.sql("UPDATE strategy_accounts SET balance=50 WHERE account_id='alice'")
    stack.sql("INSERT INTO strategy_accounts VALUES ('bob',50,50)")
    stack.sql("INSERT INTO agent_messages(run_id,agent_id,role,content) VALUES ('T2','b','research','{}')")
    result = traces.check_snapshot(manifest, *snapshot(stack))
    assert all(result["tables"].values()) and result["pending_changes"] == 3
    assert result["sealed_records"] == 2
    with psycopg.connect(stack.url, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        roots, journal, pending = traces.stream_snapshot(conn, manifest)
        assert roots == manifest["tables"] and pending == 3


@pytest.mark.parametrize("table", ["agent_messages", "strategy_trades", "ledger_events"])
def test_trace_seal_rejects_immutable_rewrites(table):
    before = {k: "k" for k in traces.TABLES[table]}
    before["value"] = "original"
    after = {**before, "value": "forged"}
    rows = {t: {} for t in traces.TABLES}
    rows[table][traces.key(table, before)] = before
    manifest = {
        "version": 1, "tables": {t: traces.row_root(rows[t]) for t in traces.TABLES},
        "journal": {"ranges": [], "count": 0, "sha256": traces.digest([])}}
    rows[table][traces.key(table, after)] = after
    with pytest.raises(ValueError, match="immutable trace rewritten"):
        traces.check_snapshot(manifest, rows, [{"id": 1, "table_name": table, "operation": "UPDATE", "before_row": before, "after_row": after}])


def test_trace_seal_detects_unlogged_edit_and_journal_rewrite(stack, monkeypatch):
    manifest = seed(stack, monkeypatch)
    rows, journal = snapshot(stack)
    altered = deepcopy(journal)
    altered[0]["after_row"]["balance"] = 999
    with pytest.raises(ValueError, match="sealed change journal differs"):
        traces.check_snapshot(manifest, deepcopy(rows), altered)
    with pytest.raises(ValueError, match="sealed change journal differs"):
        traces.check_snapshot(manifest, deepcopy(rows), journal[1:])
    # Model a privileged writer disabling the capture trigger: the external root still detects it.
    stack.sql("ALTER TABLE strategy_accounts DISABLE TRIGGER qs_audit_change")
    stack.sql("UPDATE strategy_accounts SET balance=1")
    stack.sql("ALTER TABLE strategy_accounts ENABLE TRIGGER qs_audit_change")
    with pytest.raises(ValueError, match="sealed records differ"):
        traces.check_snapshot(manifest, *snapshot(stack))


def test_trace_journal_is_atomic_append_only_and_gap_safe(stack, monkeypatch):
    manifest = seed(stack, monkeypatch)
    for query in ("DELETE FROM audit_changes", "UPDATE audit_changes SET operation='fake'", "TRUNCATE audit_changes", "TRUNCATE agent_messages"):
        with pytest.raises(psycopg.Error, match="cannot be rewritten"):
            stack.sql(query)
    with psycopg.connect(stack.url) as conn:
        conn.execute("INSERT INTO strategy_accounts VALUES ('rollback',1,1)")
        conn.rollback()
    assert len(snapshot(stack)[1]) == manifest["journal"]["count"]
    stack.sql("INSERT INTO strategy_accounts VALUES ('after-gap',1,1)")
    assert traces.check_snapshot(manifest, *snapshot(stack))["pending_changes"] == 1


def test_trace_concurrent_commit_hole_is_not_treated_as_sealed(stack, monkeypatch):
    seed(stack, monkeypatch)
    with psycopg.connect(stack.url) as conn:
        conn.execute("INSERT INTO strategy_accounts VALUES ('early-id-late-commit',1,1)")
        stack.sql("INSERT INTO strategy_accounts VALUES ('later-id-first-commit',1,1)")
        manifest = traces.make_manifest(*snapshot(stack))
        conn.commit()
    assert traces.check_snapshot(manifest, *snapshot(stack))["pending_changes"] == 1
    with psycopg.connect(stack.url, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        assert traces.stream_snapshot(conn, manifest)[2] == 1


def test_trace_latest_onchain_proof_prevents_database_rollback(stack, monkeypatch):
    manifest = seed(stack, monkeypatch)
    h = traces.digest(manifest)
    chain = {"anchor_id": manifest["anchor_id"], "manifest_hash": h, "signature": "external"}
    monkeypatch.setattr(traces, "latest_chain", lambda: chain)
    with pytest.raises(ValueError, match="latest on-chain proof is missing"):
        traces.verify(stack.url)
    stack.sql("INSERT INTO audit_anchors(anchor_id,manifest,manifest_hash,signature) VALUES (%s,%s,%s,'external')",
              (manifest["anchor_id"], Jsonb(manifest), h))
    assert traces.verify(stack.url)["ok"]
    changed = deepcopy(manifest)
    changed["created_at"] = "forged"
    stack.sql("UPDATE audit_anchors SET manifest=%s,manifest_hash=%s", (Jsonb(changed), traces.digest(changed)))
    with pytest.raises(ValueError, match="latest on-chain commitment"):
        traces.verify(stack.url)


def test_trace_marketplace_and_agent_http_writes_are_reconstructed(stack, monkeypatch):
    import httpx
    monkeypatch.setenv("QS_AUDIT_SIGNER", PUBLIC)
    token = "isolated-operator-" + "x" * 40
    stack.start_api(QS_AUTH_REQUIRED="1", QS_API_TOKEN=token)
    with httpx.Client(base_url=stack.base, headers={"Authorization": "Bearer " + token}) as c:
        baseline = traces.make_manifest(*snapshot(stack))
        body = {"request_id": "audit-buy", "listing_id": "Lnike-review-radar-v1", "quantity": 1, "account_id": "demo-buyer"}
        assert httpx.post(stack.base + "/v1/marketplace/buy", json=body).status_code == 401
        assert c.post("/v1/marketplace/buy", json=body).status_code == 200
        assert c.post("/v1/agent-messages", json={"run_id": "T-audit", "agent_id": "research", "role": "research", "content": {"claim": "recorded"}}).status_code == 200
        with psycopg.connect(stack.url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            roots, journal, pending = traces.stream_snapshot(conn, baseline)
            assert roots == baseline["tables"] and pending > 1
        sealed_trade = traces.make_manifest(*snapshot(stack))
        assert sealed_trade["tables"]["strategy_trades"]["count"] == 1
        stack.sql("UPDATE strategy_trades SET config='{}'")
        with psycopg.connect(stack.url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            with pytest.raises(ValueError, match="immutable trace rewritten: strategy_trades"):
                traces.stream_snapshot(conn, sealed_trade)


def test_trace_devnet_guard_and_signer_validation(monkeypatch):
    monkeypatch.setenv("QS_AUDIT_SIGNER", PUBLIC)
    monkeypatch.setattr(solana, "rpc", lambda *args: "mainnet-genesis")
    with pytest.raises(RuntimeError, match="non-Devnet"):
        solana.send_memo("must not sign")
    tx = {"meta": {"err": None}, "transaction": {"message": {"accountKeys": [{"pubkey": PUBLIC, "signer": True}],
        "instructions": [{"programId": solana.MEMO_PROGRAM, "parsed": traces.PREFIX + "id:hash"}]}}}
    monkeypatch.setattr(solana, "rpc", lambda *args: tx)
    assert traces.transaction_memo("sig") == traces.PREFIX + "id:hash"
    tx["transaction"]["message"]["accountKeys"][0]["pubkey"] = "attacker"
    with pytest.raises(ValueError, match="unauthorized signer"):
        traces.transaction_memo("sig")
    tx["meta"]["err"] = "failed"
    with pytest.raises(ValueError, match="failed"):
        traces.transaction_memo("sig")


def test_trace_sealer_reuses_finalized_candidate_after_crash(stack, monkeypatch, tmp_path):
    seed(stack, monkeypatch)
    monkeypatch.setenv("QS_AUDIT_RECEIPTS", str(tmp_path))
    chain = None
    sent = []
    def publish(memo):
        nonlocal chain
        sent.append(memo)
        anchor_id, h = memo[len(traces.PREFIX):].split(":")
        chain = {"anchor_id": anchor_id, "manifest_hash": h, "signature": f"signature-{len(sent)}"}
        return chain["signature"]
    monkeypatch.setattr(traces, "latest_chain", lambda: chain)
    monkeypatch.setattr(solana, "send_memo", publish)
    monkeypatch.setattr(solana, "load_keypair", lambda: type("K", (), {"pubkey": lambda self: PUBLIC})())
    monkeypatch.setattr(solana, "rpc", lambda *args: {"value": [{"confirmationStatus": "finalized", "err": None}]})
    monkeypatch.setattr(traces, "transaction_memo", lambda sig: sent[-1])
    assert traces.seal(stack.url, force=True)["ok"]
    assert len(sent) == 1 and len(list(tmp_path.glob("*.json"))) == 1
    assert traces.seal(stack.url)["status"] == "unchanged"
    # Model a crash after finalization, before persisting the landed signature.
    stack.sql("UPDATE audit_anchors SET signature=NULL")
    assert traces.seal(stack.url)["ok"]
    assert len(sent) == 1
    stack.sql("UPDATE strategy_accounts SET balance=99 WHERE account_id='alice'")
    assert traces.seal(stack.url)["ok"]
    assert len(sent) == 2
    assert stack.sql("SELECT count(*) FROM audit_anchors WHERE signature IS NOT NULL")[0][0] == 2
