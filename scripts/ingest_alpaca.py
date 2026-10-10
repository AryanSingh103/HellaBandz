"""Download historical US equity bars from Alpaca Market Data (free Basic plan) and snapshot them.

Requires ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (free account). Raw data is not
committed (provider terms); this script plus the manifest hash make the dataset reproducible.

    uv run python scripts/ingest_alpaca.py --symbols SPY QQQ AAPL MSFT NVDA --start 2023-01-03 --end 2026-09-30

Notes: the Basic plan serves SIP historical data older than 15 minutes; `--feed iex`
is available if SIP is not entitled. Bars are split/dividend adjusted (`adjustment=all`)
and filtered to regular trading hours (13:30-20:00 UTC in summer / 14:30-21:00 in winter
are approximated by the exchange_calendars-free check below: 09:30-16:00 America/New_York).
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from quantswarm.ingestion.dataset import BarTable, write_snapshot

ROOT = Path(__file__).resolve().parents[1]
URL = "https://data.alpaca.markets/v2/stocks/bars"
NY = ZoneInfo("America/New_York")


def fetch(symbols: list[str], start: str, end: str, timeframe: str, feed: str) -> list[dict]:
    key, secret = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY (free Alpaca account)")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end,
              "adjustment": "all", "feed": feed, "limit": 10_000, "sort": "asc"}
    out: list[dict] = []
    with httpx.Client(timeout=60, headers=headers) as c:
        while True:
            r = c.get(URL, params=params)
            if r.status_code == 429:
                time.sleep(3)
                continue
            r.raise_for_status()
            j = r.json()
            for sym, bars in (j.get("bars") or {}).items():
                out.extend({"S": sym, **b} for b in bars)
            tok = j.get("next_page_token")
            if not tok:
                return out
            params["page_token"] = tok


def regular_hours(ts: datetime) -> bool:
    local = ts.astimezone(NY)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 <= minutes < 16 * 60


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "AAPL", "MSFT", "NVDA"])
    ap.add_argument("--start", default="2023-01-03")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--timeframe", default="1Hour")
    ap.add_argument("--feed", default="sip", choices=["sip", "iex"])
    a = ap.parse_args()
    t0 = time.perf_counter()
    raw = fetch(a.symbols, a.start, a.end, a.timeframe, a.feed)
    rows = []
    for b in raw:
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        if a.timeframe.endswith("Hour") and not regular_hours(ts):
            continue
        px = [int(round(float(b[k]) * 1_000_000)) for k in ("o", "h", "l", "c")]
        rows.append((b["S"], int(ts.timestamp()), *px, int(b["v"])))
    t = BarTable.from_rows(rows)
    interval = {"1Hour": 3600, "1Day": 86_400}.get(a.timeframe)
    ds = f"alpaca-{a.feed}-{a.timeframe.lower()}-{a.start}-{a.end}"
    m = write_snapshot(t, ROOT / "data/snapshots" / ds, {
        "dataset_id": ds, "source": f"Alpaca Market Data v2 stocks/bars feed={a.feed}", "interval_s": interval,
        "adjustment": "all (split+dividend)", "symbols_requested": a.symbols,
        "download_seconds": round(time.perf_counter() - t0, 2)})
    print(f"{ds}: {m['row_count']} rows, sha256 {m['content_sha256'][:16]}..., warnings: {m['validation']['warnings'][:3]}")
    print(f"run a tournament on it: uv run python -m quantswarm.agents.tournament --dataset data/snapshots/{ds}")


if __name__ == "__main__":
    main()
