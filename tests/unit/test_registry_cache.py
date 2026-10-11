"""File changes must invalidate cached validation; callers cannot mutate it."""
import json
from unittest.mock import Mock

from hellabandz.ingestion import registry


def snapshot(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text(json.dumps({"splits": {"train": [1, 2]}, "source": "synthetic"}))
    (tmp_path / "bars.parquet").write_bytes(b"initial")
    load = Mock(return_value=(object(), {}))
    monkeypatch.setattr(registry, "load_snapshot", load)
    monkeypatch.setattr(registry, "validate", lambda *_: {"ok": True})
    registry._describe_cached.cache_clear()
    return load


def test_description_cache_reuses_validation_and_isolates_callers(tmp_path, monkeypatch):
    load = snapshot(tmp_path, monkeypatch)
    first = registry.describe("test", tmp_path)
    first["quality"]["ok"] = False
    assert registry.describe("test", tmp_path)["quality"]["ok"] is True
    assert load.call_count == 1
    (tmp_path / "manifest.json").write_text(json.dumps({"splits": {"train": [1, 2]}, "source": "changed manifest"}))
    assert registry.describe("test", tmp_path)["source"] == "changed manifest"
    assert load.call_count == 2


def test_changed_parquet_retries_failed_validation(tmp_path, monkeypatch):
    load = snapshot(tmp_path, monkeypatch)
    load.side_effect = ValueError("bad snapshot")
    assert registry.describe("test", tmp_path)["execution_usable"] is False
    load.side_effect = None
    assert registry.describe("test", tmp_path)["execution_usable"] is False
    (tmp_path / "bars.parquet").write_bytes(b"repaired snapshot")
    assert registry.describe("test", tmp_path)["execution_usable"] is True
    assert load.call_count == 2
