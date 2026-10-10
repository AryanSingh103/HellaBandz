"""Frozen, hashed market-data snapshots.

A snapshot is a Parquet file of normalized bars plus a manifest. The content
hash is computed over the sorted columns (not the Parquet bytes) so it is
stable across writer versions. Loading verifies the hash before use.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

COLUMNS = ("symbol", "ts", "open", "high", "low", "close", "volume")
SCHEMA_VERSION = "bars-v1"


def unit_context(meta: dict) -> dict:
    crypto = meta.get("unit_version") == "base-lot-v2"
    binance = "binance" in meta.get("source", "").lower()
    return {"quote_currency": meta.get("quote_currency", "USDT" if crypto or binance else "USD"),
            "unit_version": meta.get("unit_version", "whole-share-v1"),
            "lots_per_base_unit": 1000 if crypto else 1,
            "base_units_per_lot": "0.001" if crypto else "1",
            "quantity_label": "lots (0.001 base units each)" if crypto else ("legacy execution units" if binance else "shares"),
            "price_unit": "micro quote units per execution lot",
            "money_unit": "micro quote units; USDT is not assumed equivalent to USD"}


def validate_unit_metadata(meta: dict) -> None:
    if meta.get("unit_version") == "base-lot-v2" or meta.get("schema_version") == "bars-v2-lots":
        from decimal import Decimal, InvalidOperation
        try:
            valid = (meta.get("schema_version") == "bars-v2-lots" and meta.get("unit_version") == "base-lot-v2"
                     and type(meta.get("lots_per_base_unit")) is int and meta["lots_per_base_unit"] == 1000
                     and Decimal(str(meta.get("base_units_per_lot"))) == Decimal("0.001")
                     and meta.get("quote_currency") == "USDT")
        except InvalidOperation:
            valid = False
        if not valid:
            raise ValueError("base-lot-v2 requires bars-v2-lots, 1000 lots per base unit, 0.001 base units per lot and USDT quote currency")


@dataclass
class BarTable:
    """Columnar bars sorted by (ts, symbol). Prices are int micros, ts epoch seconds."""

    symbol: np.ndarray  # str
    ts: np.ndarray  # int64
    open: np.ndarray  # int64
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)

    @classmethod
    def from_rows(cls, rows: list[tuple]) -> "BarTable":
        rows = sorted(rows, key=lambda r: (r[1], r[0]))
        cols = list(zip(*rows)) if rows else [[] for _ in COLUMNS]
        return cls(
            np.array(cols[0], dtype=object),
            *(np.array(c, dtype=np.int64) for c in cols[1:]),
        )

    def sorted(self) -> "BarTable":
        order = np.lexsort((self.symbol.astype(str), self.ts))
        return BarTable(*(getattr(self, c)[order] for c in COLUMNS))

    def select(self, mask: np.ndarray) -> "BarTable":
        return BarTable(*(getattr(self, c)[mask] for c in COLUMNS))

    def symbols(self) -> list[str]:
        return sorted(set(self.symbol.tolist()))

    def content_hash(self, version: str = SCHEMA_VERSION) -> str:
        h = hashlib.sha256()
        h.update(version.encode())
        h.update("\x1f".join(self.symbol.astype(str).tolist()).encode())
        for c in COLUMNS[1:]:
            h.update(np.ascontiguousarray(getattr(self, c), dtype="<i8").tobytes())
        return h.hexdigest()


def validate(t: BarTable, expected_interval_s: int | None = None) -> dict:
    """Data-quality report. `ok` is False if any hard check fails; gaps are
    reported but not filled (filling would invent prices)."""
    errors: list[str] = []
    warnings: list[str] = []
    if len(t) == 0:
        errors.append("empty dataset")
    if np.any(np.diff(t.ts) < 0):
        errors.append("rows not sorted by timestamp")
    per_symbol = {}
    for sym in t.symbols():
        m = t.symbol == sym
        ts = t.ts[m]
        d = np.diff(ts)
        if np.any(d == 0):
            errors.append(f"{sym}: duplicate timestamps ({int(np.sum(d == 0))})")
        if np.any(d < 0):
            errors.append(f"{sym}: timestamps not increasing")
        o, h, lo, c, v = t.open[m], t.high[m], t.low[m], t.close[m], t.volume[m]
        bad = (lo <= 0) | (lo > np.minimum(o, c)) | (h < np.maximum(o, c))
        if np.any(bad):
            errors.append(f"{sym}: {int(bad.sum())} bars violate low<=open/close<=high or non-positive price")
        if np.any(v < 0):
            errors.append(f"{sym}: negative volume")
        gaps = 0
        if expected_interval_s:
            gaps = int(np.sum((d > expected_interval_s) & (d < 3 * 3600 * 6)))  # intraday gaps only
            if gaps:
                warnings.append(f"{sym}: {gaps} intraday gaps larger than interval")
        per_symbol[sym] = {"rows": int(m.sum()), "first_ts": int(ts[0]), "last_ts": int(ts[-1]), "intraday_gaps": gaps}
    return {"ok": not errors, "errors": errors, "warnings": warnings, "symbols": per_symbol}


def split_boundaries(t: BarTable, fractions=(0.6, 0.2, 0.2)) -> dict[str, list[int]]:
    """Chronological split on distinct timestamps: {name: [start_ts, end_ts_exclusive]}."""
    uniq = np.unique(t.ts)
    n = len(uniq)
    a = int(n * fractions[0])
    b = int(n * (fractions[0] + fractions[1]))
    end = int(uniq[-1]) + 1
    return {
        "train": [int(uniq[0]), int(uniq[a])],
        "validation": [int(uniq[a]), int(uniq[b])],
        "test": [int(uniq[b]), end],
    }


def write_snapshot(t: BarTable, out_dir: Path, meta: dict) -> dict:
    validate_unit_metadata(meta)
    if meta.get("unit_version") == "base-lot-v2" and (out_dir / "manifest.json").exists():
        raise ValueError("versioned crypto snapshots are immutable; use a new dataset ID")
    out_dir.mkdir(parents=True, exist_ok=True)
    t = t.sorted()
    report = validate(t, meta.get("interval_s"))
    if not report["ok"]:
        raise ValueError(f"dataset failed validation: {report['errors']}")
    table = pa.table({c: getattr(t, c).astype(str) if c == "symbol" else getattr(t, c) for c in COLUMNS})
    pq.write_table(table, out_dir / "bars.parquet")
    manifest = {
        "dataset_id": meta["dataset_id"],
        "schema_version": meta.get("schema_version", SCHEMA_VERSION),
        "source": meta["source"],
        "symbols": t.symbols(),
        "interval_s": meta.get("interval_s"),
        "adjustment": meta.get("adjustment", "n/a"),
        "row_count": len(t),
        "first_ts": int(t.ts[0]),
        "last_ts": int(t.ts[-1]),
        "content_sha256": t.content_hash(meta.get("schema_version", SCHEMA_VERSION)),
        "splits": split_boundaries(t),
        "validation": {"warnings": report["warnings"]},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{k: v for k, v in meta.items() if k not in ("dataset_id", "source", "interval_s", "adjustment")},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load_snapshot(path: Path) -> tuple[BarTable, dict]:
    manifest = json.loads((path / "manifest.json").read_text())
    if "binance" in manifest.get("source", "").lower() and manifest.get("unit_version") != "base-lot-v2":
        raise ValueError("legacy Binance snapshot has incompatible volume units; create a new base-lot-v2 snapshot")
    validate_unit_metadata(manifest)
    tab = pq.read_table(path / "bars.parquet")
    t = BarTable(
        np.array(tab.column("symbol").to_pylist(), dtype=object),
        *(tab.column(c).to_numpy().astype(np.int64) for c in COLUMNS[1:]),
    )
    if t.content_hash(manifest.get("schema_version", SCHEMA_VERSION)) != manifest["content_sha256"]:
        raise ValueError(f"snapshot {path} content hash mismatch; refusing to use tampered data")
    return t, manifest
