"""Local snapshot allowlist shared read-only by the API and job manager."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from hellabandz.ingestion.dataset import load_snapshot, validate

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS = Path(os.environ.get("QS_SNAPSHOTS", ROOT / "data/snapshots"))
MANIFESTS = Path(os.environ.get("QS_MANIFESTS", ROOT / "data/manifests"))


def registered() -> dict[str, Path]:
    paths = [ROOT / "data/fixtures/synthetic_1h", *sorted(SNAPSHOTS.glob("*"))]
    result = {}
    for path in paths:
        if path.is_dir() and (path / "manifest.json").is_file():
            try:
                meta = json.loads((path / "manifest.json").read_text())
                result.setdefault(meta["dataset_id"], path)
            except (ValueError, KeyError, OSError):
                continue
    return result


def resolve(dataset_id: str) -> Path:
    path = registered().get(dataset_id)
    if path is None:
        raise ValueError("unknown registered dataset ID")
    return path


def describe(dataset_id: str, path: Path) -> dict:
    path = path.resolve()
    # Hashing/validating a frozen snapshot is expensive. A changed manifest or
    # Parquet file invalidates the bounded cache, including failed validations.
    versions = []
    for name in ("manifest.json", "bars.parquet"):
        try:
            stat = (path / name).stat()
            versions.append((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:
            versions.append(None)
    return deepcopy(_describe_cached(dataset_id, path, tuple(versions)))


@lru_cache(maxsize=32)
def _describe_cached(dataset_id: str, path: Path, versions: tuple) -> dict:
    meta = json.loads((path / "manifest.json").read_text())
    try:
        table, _ = load_snapshot(path)
        quality = validate(table, meta.get("interval_s"))
        usable = quality["ok"] and all(a < b for a, b in meta["splits"].values())
        reason = None if usable else "quality checks or chronological splits failed"
    except (ValueError, OSError, KeyError) as exc:
        quality, usable, reason = {"ok": False}, False, str(exc)[:240]
    return {**meta, "dataset_id": dataset_id, "execution_usable": usable, "unavailable_reason": reason,
            "quality": quality, "quote_currency": meta.get("quote_currency", "USD"),
            "lots_per_base_unit": meta.get("lots_per_base_unit", 1),
            "synthetic": "synthetic" in meta.get("source", "").lower() or "synthetic" in dataset_id}
