"""Champion/challenger: rejected stays rejected, promotion is recorded, rollback restores the previous champion."""

import pytest

from hellabandz.trading.registry import Registry, RegistryError
from hellabandz.trading.strategies import make_version

U = ["QQQ", "SPY"]


def test_promotion_rollback_and_rejection_are_recorded(tmp_path):
    reg = Registry(tmp_path / "r.json")
    a = make_version("trend", "trend", {"fast": 20, "slow": 100}, U)
    b = make_version("trend", "trend", {"fast": 35, "slow": 200}, U)
    bad = make_version("trend", "trend", {"fast": 50, "slow": 300}, U)
    ka, kb, kbad = (reg.propose("trend", v, "test") for v in (a, b, bad))
    reg.record_evaluation(ka, {}, {}, True, ["ok"])
    reg.record_evaluation(kb, {}, {}, True, ["ok"])
    reg.record_evaluation(kbad, {}, {}, False, ["NO_EDGE"])
    with pytest.raises(RegistryError):
        reg.promote("trend", kbad, 0.3)
    with pytest.raises(RegistryError):
        reg.record_evaluation(kbad, {}, {}, True, ["try again"])  # a rejected candidate stays rejected
    reg.promote("trend", ka, 0.2)
    reg.promote("trend", kb, 0.3)
    assert Registry(tmp_path / "r.json").champions()["trend"]["version"] == b.version  # persisted atomically
    assert reg.rollback("trend")["version"] == a.version and reg.champions()["trend"]["weight"] == 0.2
    assert [h["event"] for h in reg.data["history"]][-3:] == ["promoted", "promoted", "rolled_back"]
    assert reg.view_holdout("abc") == 1 and reg.view_holdout("abc") == 2
@pytest.mark.parametrize("permanent", [False, True])
def test_atomic_replace_permission_retry_is_bounded_on_posix(monkeypatch, tmp_path, permanent):
    import json
    from types import SimpleNamespace
    from hellabandz.trading import registry as module
    real_os = module.os
    attempts, sleeps = [], []

    def replace_with_transient_permission_error(source, destination):
        attempts.append((source, destination))
        if permanent or len(attempts) <= 2:
            raise PermissionError("simulated host bind reader retained file handle")
        real_os.replace(source, destination)

    # Simulate the Linux-container branch without altering global os.name and
    # breaking Windows pathlib/tempfile behavior in the test process.
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix", fdopen=real_os.fdopen,
                        fsync=real_os.fsync, replace=replace_with_transient_permission_error,
                        path=real_os.path, unlink=real_os.unlink))
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=sleeps.append))
    reg = module.Registry(tmp_path / "registry.json")
    if permanent:
        with pytest.raises(PermissionError, match="bind reader"):
            reg._save()
        assert len(attempts) == 6
        assert sleeps == [.02, .04, .06, .08, .10]
    else:
        reg._save()
        assert len(attempts) == 3
        assert sleeps == [.02, .04]
        assert json.loads(reg.path.read_text()) == reg.data
    assert not list(tmp_path.glob(".registry-*"))
