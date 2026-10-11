"""Download Binance public spot klines, verify SHA-256 checksums, normalize, snapshot.

Source: https://data.binance.vision (public archive). Raw files are not committed;
this script reproduces the snapshot and its content hash.

    uv run python scripts/ingest_binance.py --symbol BTCUSDT --months 2024-01 2024-02 2024-03
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import time
import zipfile
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN
from pathlib import Path

import httpx

from hellabandz.ingestion.dataset import BarTable, write_snapshot

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://data.binance.vision/data/spot/monthly/klines"
SUPPORTED_SYMBOLS = ("BTCUSDT", "ETHUSDT")


def fetch(symbol: str, interval: str, month: str, raw_dir: Path) -> Path:
    name = f"{symbol}-{interval}-{month}.zip"
    url = f"{BASE}/{symbol}/{interval}/{name}"
    dst = raw_dir / name
    if not dst.exists():
        r = httpx.get(url, timeout=120, follow_redirects=True)
        r.raise_for_status()
        dst.write_bytes(r.content)
    expected = httpx.get(url + ".CHECKSUM", timeout=60, follow_redirects=True).text.split()[0]
    actual = hashlib.sha256(dst.read_bytes()).hexdigest()
    if actual != expected:
        dst.unlink()
        raise SystemExit(f"checksum mismatch for {name}: {actual} != {expected}")
    return dst


def parse(zpath: Path, symbol: str) -> list[tuple]:
    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError("supported USDT pairs are BTCUSDT and ETHUSDT")
    rows = []
    with zipfile.ZipFile(zpath) as z:
        with z.open(z.namelist()[0]) as f:
            for rec in csv.reader(io.TextIOWrapper(f)):
                if not rec[0].isdigit():
                    continue  # header row (present in some files)
                t = int(rec[0])
                # Binance spot files switched from milliseconds to microseconds in 2025
                ts = t // 1_000_000 if t > 10**14 else t // 1_000
                # One execution lot = 0.001 base units. Every price is per lot;
                # volume rounds down, so the participation cap cannot overfill.
                o, h, lo, c = (int((Decimal(x) * 1000).to_integral_value(rounding=ROUND_HALF_EVEN))
                              for x in rec[1:5])
                vol = int((Decimal(rec[5]) * 1000).to_integral_value(rounding=ROUND_FLOOR))
                rows.append((symbol, ts, o, h, lo, c, vol))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT", choices=SUPPORTED_SYMBOLS)
    ap.add_argument("--interval", default="1m", choices=("1m",))
    ap.add_argument("--months", nargs="+", default=["2024-01", "2024-02", "2024-03"])
    a = ap.parse_args()
    raw = ROOT / "data/raw/binance"
    raw.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    files = [fetch(a.symbol, a.interval, m, raw) for m in a.months]
    t_dl = time.perf_counter() - t0
    rows = [r for f in files for r in parse(f, a.symbol)]
    t = BarTable.from_rows(rows)
    dataset_id = f"binance-{a.symbol.lower()}-{a.interval}-{a.months[0]}-{a.months[-1]}-lot-v2"
    m = write_snapshot(t, ROOT / "data/snapshots" / dataset_id, {
        "dataset_id": dataset_id, "source": f"data.binance.vision spot klines {a.symbol} {a.interval}",
        "interval_s": 60, "adjustment": "none (spot)", "files": [f.name for f in files],
        "schema_version": "bars-v2-lots", "unit_version": "base-lot-v2", "quote_currency": "USDT",
        "base_units_per_lot": "0.001", "lots_per_base_unit": 1000,
        "units": "prices in micro-USDT per 0.001-base-asset lot; volume and quantities in integer lots"})
    print(f"{dataset_id}: {m['row_count']} rows, sha256 {m['content_sha256'][:16]}..., download+verify {t_dl:.1f}s")


if __name__ == "__main__":
    main()
