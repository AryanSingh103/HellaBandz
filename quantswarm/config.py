"""Load optional settings from a `.env` file at the repo root.

Values already present in the environment win, so `docker compose` and CI can
override anything. No third-party dependency; supports `KEY=value`, comments,
blank lines, optional `export` and quotes, and trailing `# comments` on
unquoted values.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or ROOT / ".env"
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if not key or not val or key in os.environ:
            continue  # empty values mean "unset"; real environment wins
        os.environ[key] = val
        loaded[key] = val
    return loaded
