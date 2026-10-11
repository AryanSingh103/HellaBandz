"""Champion/challenger registry for runtime strategy versions.

Lifecycle of a version:  candidate -> (evaluated) accepted | rejected -> promoted champion -> retired.

- Research writes candidates. Evaluation records the metrics and the decision against explicit
  acceptance criteria. A rejected candidate stays rejected: promote() refuses it.
- Promotion and rollback replace the whole registry file atomically (write a temp file, fsync,
  os.replace), and append an entry to the history, so a crash leaves either the old or the new state.
- Rollback restores the previous champion recorded in the history.
- Every evaluation of the held-out period is counted per dataset. After the first, reports say the
  period has been inspected and is no longer an untouched test.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hellabandz.trading.strategies import StrategyVersion

DEFAULT_PATH = Path(os.environ.get("QS_REGISTRY", Path(__file__).resolve().parents[2] / "data/registry/coordinated.json"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RegistryError(RuntimeError):
    pass


class Registry:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "versions": {}, "champions": {}, "history": [], "holdout_views": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".registry-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _event(self, kind: str, **kw) -> None:
        self.data["history"].append({"at": _now(), "event": kind, **kw})

    def propose(self, role: str, v: StrategyVersion, source: str) -> str:
        key = f"{role}:{v.version}"
        if key not in self.data["versions"]:
            self.data["versions"][key] = {"role": role, "status": "candidate", "source": source, **v.describe()}
            self._event("proposed", role=role, version=v.version)
            self._save()
        return key

    def record_evaluation(self, key: str, metrics: dict, criteria: dict, accepted: bool, reasons: list[str]) -> None:
        rec = self.data["versions"][key]
        if rec["status"] == "rejected" and accepted:
            raise RegistryError(f"{key} was rejected before; a rejected candidate stays rejected")
        rec.update(status="accepted" if accepted else "rejected", evaluation=metrics, criteria=criteria,
                   decision_reasons=reasons)
        self._event("accepted" if accepted else "rejected", role=rec["role"], version=rec["version"], reasons=reasons)
        self._save()

    def promote(self, role: str, key: str, weight: float) -> None:
        rec = self.data["versions"].get(key)
        if rec is None or rec["status"] not in ("accepted", "champion", "retired"):
            raise RegistryError(f"{key} is not an accepted version")
        prev = self.data["champions"].get(role)
        if prev and prev["key"] == key and prev["weight"] == weight:
            return
        if prev:
            self.data["versions"][prev["key"]]["status"] = "retired"
        rec["status"] = "champion"
        self.data["champions"][role] = {"key": key, "version": rec["version"], "weight": weight, "since": _now()}
        self._event("promoted", role=role, version=rec["version"], weight=weight, previous=prev and prev["version"])
        self._save()

    def demote(self, role: str, reason: str) -> None:
        prev = self.data["champions"].pop(role, None)
        if prev:
            self.data["versions"][prev["key"]]["status"] = "retired"
            self._event("demoted", role=role, version=prev["version"], reason=reason)
            self._save()

    def rollback(self, role: str) -> dict | None:
        """Restore the champion that was active before the latest promotion of this role."""
        promos = [h for h in self.data["history"] if h["event"] == "promoted" and h["role"] == role]
        if not promos or not promos[-1].get("previous"):
            raise RegistryError(f"no previous champion for {role}")
        prev_version = promos[-1]["previous"]
        key = f"{role}:{prev_version}"
        weight = next(h["weight"] for h in reversed(promos[:-1]) if h["version"] == prev_version)
        cur = self.data["champions"].get(role)
        if cur:
            self.data["versions"][cur["key"]]["status"] = "retired"
        self.data["versions"][key]["status"] = "champion"
        self.data["champions"][role] = {"key": key, "version": prev_version, "weight": weight, "since": _now()}
        self._event("rolled_back", role=role, version=prev_version, replaced=cur and cur["version"])
        self._save()
        return self.data["champions"][role]

    def champions(self) -> dict[str, dict]:
        return {r: {**c, **{k: self.data["versions"][c["key"]][k] for k in ("family", "params", "universe")}}
                for r, c in sorted(self.data["champions"].items())}

    def view_holdout(self, dataset_sha: str) -> int:
        n = self.data["holdout_views"].get(dataset_sha, 0) + 1
        self.data["holdout_views"][dataset_sha] = n
        self._event("holdout_evaluated", dataset=dataset_sha[:16], views=n)
        self._save()
        return n
