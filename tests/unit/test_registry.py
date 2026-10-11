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
