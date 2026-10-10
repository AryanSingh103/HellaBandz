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
from pathlib import Path

import httpx

from quantswarm.ingestion.dataset import BarTable, write_snapshot

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://data.binance.vision/data/spot/monthly/klines"


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
    rows = []
    with zipfile.ZipFile(zpath) as z:
        with z.open(z.namelist()[0]) as f:
            for rec in csv.reader(io.TextIOWrapper(f)):
                if not rec[0].isdigit():
                    continue  # header row (present in some files)
                t = int(rec[0])
                # Binance spot files switched from milliseconds to microseconds in 2025
                ts = t // 1_000_000 if t > 10**14 else t // 1_000
                o, h, lo, c = (int(round(float(x) * 1_000_000)) for x in rec[1:5])
                vol = int(float(rec[5]) * 1_000)  # base-asset volume in milli-units (whole-unit engine)
                rows.append((symbol, ts, o, h, lo, c, vol))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--months", nargs="+", default=["2024-01", "2024-02", "2024-03"])
    a = ap.parse_args()
    raw = ROOT / "data/raw/binance"
    raw.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    files = [fetch(a.symbol, a.interval, m, raw) for m in a.months]
    t_dl = time.perf_counter() - t0
    rows = [r for f in files for r in parse(f, a.symbol)]
    t = BarTable.from_rows(rows)
    dataset_id = f"binance-{a.symbol.lower()}-{a.interval}-{a.months[0]}-{a.months[-1]}"
    m = write_snapshot(t, ROOT / "data/snapshots" / dataset_id, {
        "dataset_id": dataset_id, "source": f"data.binance.vision spot klines {a.symbol} {a.interval}",
        "interval_s": 60, "adjustment": "none (spot)", "files": [f.name for f in files],
        "units": "prices in micro-USDT; volume in 0.001 base units"})
    print(f"{dataset_id}: {m['row_count']} rows, sha256 {m['content_sha256'][:16]}..., download+verify {t_dl:.1f}s")


if __name__ == "__main__":
    main()
