"""External integrations exercised against mocked services (no keys or network needed).

Covers the code paths that only run when OPENAI_API_KEY / ELEVENLABS_API_KEY /
Alpaca keys / a funded Solana key are present, so they work on first use.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from quantswarm.agents import llm, session, tournament
from quantswarm.config import load_env

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = {"symbols": {s: {} for s in ["AAPL", "MSFT", "NVDA", "QQQ", "SPY"]}}
GOOD = {"family": "sma_crossover", "universe": ["SPY", "QQQ"], "param_grid": {"fast": [10, 20], "slow": [100]},
        "rationale": "trend", "evidence": ["x"]}
META = {"model": "mock", "latency_s": 0.0}


@pytest.fixture
def fake_llm(monkeypatch):
    """Queue of responses (dicts) or exceptions returned by llm.chat_json."""
    calls: list[tuple[str, str]] = []
    queue: list = []

    def chat_json(system, user, **kw):
        calls.append((system, user))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, META

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat_json", chat_json)
    return queue, calls


# ----------------------------------------------------------------------------- OpenAI: research agents


def test_research_uses_valid_llm_proposal(fake_llm):
    queue, calls = fake_llm
    queue.append(GOOD)
    rec = tournament.research("momentum", SUMMARY, "T")
    assert rec["source"] == "llm" and rec["attempts"] == 1
    assert rec["proposal"]["universe"] == ["QQQ", "SPY"]
    assert "sma_crossover" in calls[0][0] and "TRAINING" in calls[0][0]


def test_research_retries_once_with_the_validation_error(fake_llm):
    queue, calls = fake_llm
    queue.extend([{**GOOD, "family": "zscore_reversion"}, GOOD])  # momentum may not use zscore
    rec = tournament.research("momentum", SUMMARY, "T")
    assert rec["source"] == "llm" and rec["attempts"] == 2
    assert "not allowed" in calls[1][1]


@pytest.mark.parametrize("bad", [
    {**GOOD, "universe": ["TSLA"]},                       # symbol not in dataset
    {**GOOD, "param_grid": {"fast": [500], "slow": [10]}},  # nothing inside bounds
    {**GOOD, "family": "write_my_own_python"},             # not whitelisted
    ["not", "an", "object"],
    {"rationale": "missing keys"},
])
def test_research_falls_back_to_fixture_after_two_bad_answers(fake_llm, bad):
    queue, _ = fake_llm
    queue.extend([bad, bad])
    rec = tournament.research("momentum", SUMMARY, "T")
    assert rec["source"] == "fixture" and len(rec["errors"]) == 2


def test_research_network_error_falls_back_without_retry(fake_llm):
    queue, calls = fake_llm
    queue.append(ConnectionError("down"))
    rec = tournament.research("volatility", SUMMARY, "T")
    assert rec["source"] == "fixture" and len(calls) == 1 and "ConnectionError" in rec["errors"][0]


def test_fixture_fallback_adapts_universe_to_dataset(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    rec = tournament.research("momentum", {"symbols": {"BTCUSDT": {}}}, "T")
    assert rec["proposal"]["universe"] == ["BTCUSDT"]


# ----------------------------------------------------------------------------- OpenAI: supervisor


RANKED = [{"strategy_id": "a", "family": "f", "params": {}, "validation": {}, "stability": 1, "score": 0.6, "gates": []},
          {"strategy_id": "b", "family": "f", "params": {}, "validation": {}, "stability": 1, "score": 0.0,
           "gates": ["n_fills 3 < 10"]}]


def test_supervisor_llm_weights_are_sanitized(fake_llm):
    queue, _ = fake_llm
    queue.append({"weights": {"a": 0.9, "b": "lots", "c": 0.5}, "rationale": "r"})
    sup = tournament.supervise(RANKED)
    assert sup["source"] == "llm" and sup["weights"] == {"a": 0.9, "c": 0.5}
    w, _ = tournament.allocate({"a": 0.6, "b": 0.0}, sup["weights"])
    assert w["b"] == 0.0 and "c" not in w


def test_supervisor_error_falls_back(fake_llm):
    queue, _ = fake_llm
    queue.append(TimeoutError("slow"))
    sup = tournament.supervise(RANKED)
    assert sup["source"] == "fallback" and "TimeoutError" in sup["error"]


# ----------------------------------------------------------------------------- OpenAI: crisis + ask


def test_crisis_decision_validated_and_clamped(fake_llm):
    queue, _ = fake_llm
    queue.append({"action": "buy_dip", "fraction": 7, "rationale": "yolo"})
    d = session.decide_crisis("momentum", {"SPY": 3}, 4000.0, {"SPY": 500.0})
    assert d["source"] == "llm" and d["fraction"] == 1.0
    queue.append({"action": "short_everything", "fraction": 1})
    d = session.decide_crisis("momentum", {}, 4000.0, {})
    assert d["source"].startswith("fallback") and d["action"] == "buy_dip"


def test_ask_uses_llm_only_to_phrase_facts(fake_llm):
    from quantswarm.agents.ask import answer
    queue, calls = fake_llm
    queue.append({"answer": "Equity is $10,000."})
    ctx = {"portfolio": {"state": {"cash": 10_000_000_000, "market_value": 0, "positions": {}, "orders": {}},
                         "equity": 10_000_000_000, "digest": "ab" * 32},
           "recent_orders": [], "rejections": [], "tournament": None}
    r = answer("what is our equity?", ctx)
    assert r["mode"] == "llm" and r["answer"] == "Equity is $10,000."
    assert "10000.0" in calls[0][1]  # facts were sent, not raw tables
    queue.append(RuntimeError("quota"))
    r = answer("what is our equity?", ctx)
    assert r["mode"].startswith("deterministic") and "$10,000.00" in r["answer"]


def test_reasoning_models_get_no_temperature(monkeypatch):
    sent = {}

    class FakeCompletions:
        def create(self, **kw):
            sent.update(kw)
            msg = type("M", (), {"content": "{}"})
            return type("R", (), {"choices": [type("C", (), {"message": msg})], "model": kw["model"], "usage": None})

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})

    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(llm, "MODEL", "gpt-5-mini")
    llm.chat_json("s", "u")
    assert "temperature" not in sent
    monkeypatch.setattr(llm, "MODEL", "gpt-4.1-mini")
    llm.chat_json("s", "u")
    assert sent["temperature"] == 0.4 and sent["response_format"] == {"type": "json_object"}


# ----------------------------------------------------------------------------- .env loading


def test_load_env_parses_and_never_overrides(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('# c\nexport QS_T_A="quoted value"\nQS_T_B=plain # comment\nQS_T_C=\nQS_T_D=keep\n')
    monkeypatch.setenv("QS_T_D", "from-env")
    for k in ("QS_T_A", "QS_T_B", "QS_T_C"):
        monkeypatch.delenv(k, raising=False)
    loaded = load_env(p)
    import os
    assert os.environ["QS_T_A"] == "quoted value" and os.environ["QS_T_B"] == "plain"
    assert "QS_T_C" not in os.environ and os.environ["QS_T_D"] == "from-env" and "QS_T_D" not in loaded
    for k in ("QS_T_A", "QS_T_B"):
        monkeypatch.delenv(k)


# ----------------------------------------------------------------------------- Alpaca


def _alpaca():
    spec = importlib.util.spec_from_file_location("ingest_alpaca", ROOT / "scripts/ingest_alpaca.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_alpaca_pagination_rate_limit_and_rows(monkeypatch):
    mod = _alpaca()
    pages = [
        (429, {}),
        (200, {"bars": {"SPY": [{"t": "2025-01-06T13:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                                {"t": "2025-01-06T14:00:00Z", "o": 590.1, "h": 591, "l": 589.5, "c": 590.5, "v": 1000},
                                {"t": "2025-01-06T21:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]},
               "next_page_token": "p2"}),
        (200, {"bars": {"AAPL": [{"t": "2025-01-06T15:00:00Z", "o": 245, "h": 246, "l": 244, "c": 245.5, "v": 500}]},
               "next_page_token": None}),
    ]
    seen = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

        def raise_for_status(self):
            assert self.status_code == 200

    class Client:
        def __init__(self, **kw):
            assert kw["headers"]["APCA-API-KEY-ID"] == "k"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params):
            seen.append(dict(params))
            return Resp(*pages.pop(0))

    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setattr(mod.httpx, "Client", Client)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    raw = mod.fetch(["SPY", "AAPL"], "2025-01-06", "2025-01-07", "1Hour", "sip")
    assert len(raw) == 4 and seen[-1]["page_token"] == "p2" and seen[0]["adjustment"] == "all"
    rows = mod.to_rows(raw, "1Hour")
    # EST: 13:00Z = 08:00 NY (pre-market) dropped; 14:00Z = 09:00 bar holds the 09:30 open, kept;
    # 21:00Z = 16:00 NY (after the close) dropped
    assert [(r[0], r[1]) for r in rows] == [("SPY", 1736172000), ("AAPL", 1736175600)]
    assert rows[0][2:4] == (590_100_000, 591_000_000)


def test_alpaca_bad_credentials_message(monkeypatch):
    mod = _alpaca()

    class Client:
        def __init__(self, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params):
            return type("R", (), {"status_code": 403, "text": "subscription does not permit querying recent SIP data"})

    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setattr(mod.httpx, "Client", Client)
    with pytest.raises(SystemExit, match="--feed iex"):
        mod.fetch(["SPY"], "2025-01-06", "2025-01-07", "1Hour", "sip")


# ----------------------------------------------------------------------------- ElevenLabs


def test_speak_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from quantswarm.api import app as api_mod
    client = TestClient(api_mod.app)  # no lifespan: endpoint does not touch the DB
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert client.post("/v1/speak", json={"text": "hi"}).status_code == 501

    sent = {}

    class AC:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers, json):
            sent.update(url=url, headers=headers, json=json)
            return type("R", (), {"status_code": 200, "content": b"ID3fake-mp3"})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", AC)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi")
    r = client.post("/v1/speak", json={"text": "Equity is ten thousand dollars."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3fake-mp3"
    assert sent["headers"]["xi-api-key"] == "xi" and "/v1/text-to-speech/" in sent["url"]


# ----------------------------------------------------------------------------- Solana


def test_solana_commit_builds_signed_memo_and_verify_reads_it(tmp_path, monkeypatch):
    from solders.hash import Hash
    from solders.transaction import Transaction

    from quantswarm.audit import solana as sol
    monkeypatch.setattr(sol, "KEYPAIR", tmp_path / "kp.json")
    sent = {}

    def rpc(method, params):
        if method == "getBalance":
            return {"value": 10**9}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default())}}
        if method == "sendTransaction":
            sent["tx"] = Transaction.from_bytes(base64.b64decode(params[0]))
            return str(sent["tx"].signatures[0])
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        if method == "getTransaction":
            memo = bytes(sent["tx"].message.instructions[0].data).decode()
            return {"transaction": {"message": {"instructions": [{"programId": sol.MEMO_PROGRAM, "parsed": memo}]}}}
        raise AssertionError(method)

    monkeypatch.setattr(sol, "rpc", rpc)
    manifest = tmp_path / "T-x.json"
    manifest.write_text(json.dumps({"run_id": "T-x", "allocations": {"a": 0.4}}))
    rec = sol.commit(manifest)
    tx = sent["tx"]
    tx.verify()  # signature is valid for the message
    assert str(tx.message.account_keys[tx.message.instructions[0].program_id_index]) == sol.MEMO_PROGRAM
    assert rec["memo"] == f"quantswarm:v1:T-x:{rec['manifest_sha256']}"
    assert sol.verify(manifest)["ok"]
    manifest.write_text(json.dumps({"run_id": "T-x", "allocations": {"a": 0.5}}))
    out = sol.verify(manifest)
    assert not out["ok"] and out["onchain_memo"] == rec["memo"]
