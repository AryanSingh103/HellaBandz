"""Report which optional integrations are configured and smoke-test each with one cheap live call.

    uv run python scripts/check_integrations.py          # or: make check-integrations

Reads keys from the environment or `.env`. Nothing here places orders or spends real money
(OpenAI/ElevenLabs calls are a few tokens/characters; Solana is Devnet).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantswarm.config import load_env  # noqa: E402

load_env()


def check_openai() -> tuple[str, str]:
    if not os.environ.get("OPENAI_API_KEY"):
        return "SKIP", "OPENAI_API_KEY not set (agents use recorded fixture proposals)"
    from quantswarm.agents import llm
    t = time.perf_counter()
    out, meta = llm.chat_json("Reply with JSON.", 'Return {"ok": true}.', timeout=30)
    return ("OK" if out.get("ok") is True else "WARN"), f"model {meta['model']}, {time.perf_counter() - t:.1f}s"


def check_elevenlabs() -> tuple[str, str]:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return "SKIP", "ELEVENLABS_API_KEY not set (browser speech fallback)"
    voice = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")
    r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}", timeout=30,
                   headers={"xi-api-key": key, "accept": "audio/mpeg"},
                   json={"text": "QuantSwarm online.", "model_id": os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")})
    if r.status_code != 200:
        return "FAIL", f"HTTP {r.status_code}: {r.text[:160]}"
    return "OK", f"{len(r.content):,} bytes of audio"


def check_alpaca() -> tuple[str, str]:
    k, s = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not (k and s):
        return "SKIP", "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set (synthetic fixture is used)"
    out = []
    for feed in ("sip", "iex"):
        r = httpx.get("https://data.alpaca.markets/v2/stocks/bars", timeout=30,
                      headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
                      params={"symbols": "SPY", "timeframe": "1Hour", "start": "2025-01-06", "end": "2025-01-07",
                              "feed": feed, "adjustment": "all"})
        n = len((r.json().get("bars") or {}).get("SPY", [])) if r.status_code == 200 else 0
        out.append(f"{feed}: {'ok, ' + str(n) + ' bars' if r.status_code == 200 else 'HTTP ' + str(r.status_code)}")
    status = "OK" if out[0].startswith("sip: ok") else ("WARN" if "iex: ok" in out[1] else "FAIL")
    hint = "" if status == "OK" else " -> use: scripts/ingest_alpaca.py --feed iex" if status == "WARN" else ""
    return status, "; ".join(out) + hint


def check_solana() -> tuple[str, str]:
    from quantswarm.audit import solana as sol
    kp = sol.load_keypair()
    bal = sol.rpc("getBalance", [str(kp.pubkey())])["value"]
    if bal < 1_000_000:
        return "TODO", (f"Devnet key {kp.pubkey()} has {bal / 1e9:.4f} SOL. Fund it at https://faucet.solana.com "
                        "(paste the address, choose Devnet), then run `make solana-commit`.")
    return "OK", f"Devnet key {kp.pubkey()} has {bal / 1e9:.4f} SOL ({sol.RPC})"


def check_database() -> tuple[str, str]:
    import psycopg
    from quantswarm.execution.db import database_url
    url = database_url()
    where = "Tiger Cloud" if "tsdb.cloud.timescale.com" in url or "tigerdata" in url else url.split("@")[-1]
    with psycopg.connect(url, connect_timeout=5) as c:
        v = c.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
    return ("OK" if v else "WARN"), f"{where}: timescaledb {v[0] if v else 'NOT INSTALLED'}"


def main() -> int:
    rows = []
    for name, fn in [("TimescaleDB / Tiger Data", check_database), ("OpenAI", check_openai),
                     ("ElevenLabs", check_elevenlabs), ("Alpaca", check_alpaca), ("Solana Devnet", check_solana)]:
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "FAIL", f"{type(e).__name__}: {str(e)[:200]}"
        rows.append((name, status, detail))
        print(f"{status:5} {name:26} {detail}")
    return 1 if any(s == "FAIL" for _, s, _ in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
