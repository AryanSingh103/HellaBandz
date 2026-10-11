"""Run the research tournament on independent datasets and compare its held-out result with the
equal-weight buy-and-hold baseline.

Each synthetic seed is a separate market drawn from the same generator as the committed fixture, so the
spread across seeds shows how much of a single held-out result is luck. Every run uses the production
tournament and baseline code with the recorded proposals (no LLM), is evaluated on its own held-out
split exactly once, and is never used to tune anything.

    uv run python scripts/evaluate_vs_baseline.py --seeds 101-130 --out artifacts/strategy-eval/final.json
    uv run python scripts/evaluate_vs_baseline.py --dataset data/snapshots/<id> --out artifacts/strategy-eval/real.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path


def evaluate(job: tuple[str, int | None, str]) -> dict:
    kind, seed, location = job
    work = Path(location)
    from hellabandz.agents.baseline import save_analytics
    from hellabandz.agents.tournament import run
    from hellabandz.ingestion.dataset import write_snapshot

    started = time.time()
    if kind == "seed":
        from hellabandz.ingestion.synthetic import generate
        dataset = work / "snapshot"
        write_snapshot(generate(seed=seed), dataset, {"dataset_id": f"synthetic-1h-seed{seed}", "interval_s": 3600,
                                                      "source": f"hellabandz.ingestion.synthetic (seed={seed})"})
        label = f"seed {seed}"
    else:
        dataset, label = Path(location), Path(location).name
    manifest = run(dataset, f"E{seed}" if kind == "seed" else "Ereal")["manifest"]
    baseline = save_analytics(manifest, dataset)["baseline_metrics"]
    test = manifest["held_out_test"]
    by_strategy = manifest["allocations"]["by_strategy"]
    core_id = manifest["core"]["spec"]["strategy_id"]
    return {"label": label, "seed": seed, "portfolio": test["portfolio"]["metrics"], "baseline": baseline,
            "core_weight": by_strategy[core_id]["weight"],
            "strategies": [{"agent": r["agent_id"], "family": r["family"], "params": r["params"], "score": r["score"],
                            "gates": r["gates"], "weight": by_strategy[r["strategy_id"]]["weight"], "excess": r["excess"],
                            "test_standalone": test["per_strategy_standalone"][r["strategy_id"]]}
                           for r in manifest["ranked"]],
            "core_standalone": test["core_standalone"], "seconds": round(time.time() - started, 1)}


def summarize(rows: list[dict]) -> dict:
    def col(key: str, field: str) -> list[float]:
        return [r[key][field] for r in rows]

    n = len(rows)
    excess = [a - b for a, b in zip(col("portfolio", "net_return"), col("baseline", "net_return"))]
    mean = sum(excess) / n
    se = math.sqrt(sum((x - mean) ** 2 for x in excess) / (n - 1) / n) if n > 1 else None
    return {
        "datasets": n,
        "mean_portfolio_return": sum(col("portfolio", "net_return")) / n,
        "mean_baseline_return": sum(col("baseline", "net_return")) / n,
        "mean_excess_return": mean, "excess_standard_error": se,
        "beat_baseline_on_return": sum(x > 0 for x in excess),
        "mean_portfolio_sharpe": sum(col("portfolio", "sharpe")) / n,
        "mean_baseline_sharpe": sum(col("baseline", "sharpe")) / n,
        "beat_baseline_on_sharpe": sum(a > b for a, b in zip(col("portfolio", "sharpe"), col("baseline", "sharpe"))),
        "mean_portfolio_max_drawdown": sum(col("portfolio", "max_drawdown")) / n,
        "mean_baseline_max_drawdown": sum(col("baseline", "max_drawdown")) / n,
        "lower_drawdown_than_baseline": sum(a < b for a, b in zip(col("portfolio", "max_drawdown"),
                                                                 col("baseline", "max_drawdown"))),
        "mean_active_weight": sum(sum(s["weight"] for s in r["strategies"]) for r in rows) / n,
    }


def report(rows: list[dict], summary: dict) -> None:
    print(f"{'dataset':>12} {'portfolio':>21} {'baseline':>21} {'excess':>8}  active strategies (weight)")
    for r in rows:
        p, b = r["portfolio"], r["baseline"]
        active = ", ".join(f"{s['agent']}:{s['family']} {s['weight']:.2f}" for s in r["strategies"] if s["weight"] > 0)
        print(f"{r['label']:>12} {p['net_return']:>+8.2%} {p['sharpe']:>+5.2f} {p['max_drawdown']:>6.1%} "
              f"{b['net_return']:>+8.2%} {b['sharpe']:>+5.2f} {b['max_drawdown']:>6.1%} "
              f"{p['net_return'] - b['net_return']:>+8.2%}  {active or 'none'} | core {r['core_weight']:.2f}")
    s = summary
    se = f" (standard error {s['excess_standard_error']:.2%})" if s["excess_standard_error"] is not None else ""
    print(f"\n{s['datasets']} datasets (columns: return, Sharpe, max drawdown)")
    print(f"return:   portfolio {s['mean_portfolio_return']:+.2%}  baseline {s['mean_baseline_return']:+.2%}  "
          f"excess {s['mean_excess_return']:+.2%}{se}  ahead in {s['beat_baseline_on_return']}/{s['datasets']}")
    print(f"Sharpe:   portfolio {s['mean_portfolio_sharpe']:+.2f}  baseline {s['mean_baseline_sharpe']:+.2f}  "
          f"ahead in {s['beat_baseline_on_sharpe']}/{s['datasets']}")
    print(f"drawdown: portfolio {s['mean_portfolio_max_drawdown']:.2%}  baseline {s['mean_baseline_max_drawdown']:.2%}  "
          f"lower in {s['lower_drawdown_than_baseline']}/{s['datasets']}")
    print(f"mean capital in active strategies: {s['mean_active_weight']:.0%}")


def parse_seeds(text: str) -> list[int]:
    seeds: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        seeds.extend(range(int(lo), int(hi or lo) + 1))
    return seeds


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", help="synthetic generator seeds, e.g. 101-130 or 3,7,9")
    ap.add_argument("--dataset", action="append", default=[], help="registered snapshot directory (repeatable)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--out", help="write per-dataset rows and the summary as JSON")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="qs-eval-") as tmp:
        # workers inherit these: recorded proposals and the deterministic supervisor only, scratch manifests
        os.environ.update(OPENAI_API_KEY="", QS_MANIFESTS=str(Path(tmp) / "manifests"))
        os.environ.setdefault("QS_TOURNAMENT_PROCS", "1")
        jobs =[("seed", s, str(Path(tmp) / f"seed-{s}")) for s in (parse_seeds(a.seeds) if a.seeds else [])]
        jobs += [("dataset", None, str(Path(d).resolve())) for d in a.dataset]
        if not jobs:
            ap.error("give --seeds and/or --dataset")
        with ProcessPoolExecutor(max_workers=min(a.workers, len(jobs))) as pool:
            rows = list(pool.map(evaluate, jobs))
    summary = summarize(rows)
    report(rows, summary)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
