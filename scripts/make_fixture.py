"""Generate the committed synthetic fixture: data/fixtures/synthetic_1h."""

from pathlib import Path

from hellabandz.ingestion.dataset import validate, write_snapshot
from hellabandz.ingestion.synthetic import generate

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    t = generate(seed=42)
    report = validate(t, 3600)
    print("validation ok:", report["ok"], report["errors"][:3])
    m = write_snapshot(
        t,
        ROOT / "data/fixtures/synthetic_1h",
        {"dataset_id": "synthetic-1h-seed42", "source": "hellabandz.ingestion.synthetic (seed=42)",
         "interval_s": 3600, "adjustment": "n/a (synthetic)", "seed": 42},
    )
    print(f"{m['dataset_id']}: {m['row_count']} rows, sha256={m['content_sha256'][:16]}..., splits={m['splits']}")
