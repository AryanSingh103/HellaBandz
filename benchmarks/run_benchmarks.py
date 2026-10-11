"""Reproducible, resource-bounded benchmarks. See docs/PERFORMANCE.md for methodology.

    uv run python benchmarks/run_benchmarks.py engine      # in-process: risk latency, command latency, replay speed
    uv run python benchmarks/run_benchmarks.py db          # API + worker + TimescaleDB in Docker with CPU/mem limits
    uv run python benchmarks/run_benchmarks.py recovery    # restart/replay time vs ledger size
    uv run python benchmarks/report.py                     # regenerate docs/PERFORMANCE.md

LLM latency and data download time are never included in these numbers.
"""

from __future__ import annotations

import argparse
import asyncio
from bisect import bisect_right
import json
import hashlib
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantswarm.backtester import SnapshotProvider, Slot, run_backtest  # noqa: E402
from quantswarm.contracts import (  # noqa: E402
    MICROS,
    ApplyBar,
    Bar,
    OpenPortfolio,
    OrderRequest,
    SetAllocation,
    StrategySpec,
    SubmitOrder,
)
from quantswarm.ingestion.dataset import load_snapshot  # noqa: E402
from quantswarm.ledger import EngineConfig, PortfolioState, evaluate_order, process, replay  # noqa: E402

RESULTS = ROOT / "benchmarks/results"
BINANCE_SNAPSHOT = ROOT / "data/snapshots/binance-btcusdt-1m-2024-01-2024-03-lot-v2"
WARMUP, REPS = 3, 5
SYMS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
PX = {"SPY": 500, "QQQ": 420, "AAPL": 190, "MSFT": 410, "NVDA": 120}
T0 = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
CHUNK = 200  # orders between bars
AGENTS_DB = ["a1", "a2", "a3", "a4", "a5"]


def bar_cmds(k: int) -> list:
    ts = T0 + timedelta(hours=k)
    return [ApplyBar(bar=Bar(symbol=s, ts=ts, open=PX[s] * MICROS, high=PX[s] * MICROS, low=PX[s] * MICROS,
                             close=PX[s] * MICROS, volume=10**9)) for s in SYMS]


def bar_json(k: int) -> list[dict]:
    return [json.loads(b.bar.model_dump_json()) for b in bar_cmds(k)]


def pct(xs: list[float]) -> dict:
    a = np.array(xs)
    return {"n": len(xs), "p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95)),
            "p99": float(np.percentile(a, 99)), "p999": float(np.percentile(a, 99.9)), "max": float(a.max()),
            "mean": float(a.mean())}


def summarize_reps(reps: list[dict]) -> dict:
    keys = reps[0].keys()
    return {k: {"median": statistics.median(r[k] for r in reps), "min": min(r[k] for r in reps),
                "max": max(r[k] for r in reps)} for k in keys if isinstance(reps[0][k], (int, float))}


def environment() -> dict:
    env = {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(),
           "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "operator_auth_required": os.getenv("QS_AUTH_REQUIRED", "1") != "0",
           "transactional_audit_journal": True}
    env["git_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    sources = sorted([*ROOT.glob("quantswarm/**/*.py"), *ROOT.glob("quantswarm/**/*.sql"),
                      *ROOT.glob("benchmarks/*.py"), ROOT / "Dockerfile", ROOT / "docker-compose.yml",
                      ROOT / "pyproject.toml", ROOT / "uv.lock"])
    h = hashlib.sha256()
    for path in sources:
        h.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n"))
    env["source_sha256"] = h.hexdigest()
    try:
        env["host_cpu"] = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        env["host_ncpu"] = int(subprocess.check_output(["sysctl", "-n", "hw.ncpu"], text=True))
        env["host_mem_gb"] = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) / 2**30
    except Exception:
        env["host_ncpu"] = os.cpu_count()
        if platform.system() == "Windows":
            host = json.loads(subprocess.check_output([
                "powershell", "-NoProfile", "-Command",
                "@{cpu=(Get-CimInstance Win32_Processor).Name; memory=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory} | ConvertTo-Json"
            ], text=True))
            env["host_cpu"] = host["cpu"]
            env["host_mem_gb"] = host["memory"] / 2**30
    try:
        info = json.loads(subprocess.check_output(["docker", "info", "--format", "{{json .}}"], text=True))
        env["docker_vm_ncpu"] = info.get("NCPU")
        env["docker_vm_mem_gb"] = round(info.get("MemTotal", 0) / 2**30, 2)
        env["docker_server"] = info.get("ServerVersion")
    except Exception:
        pass
    return env


def save(name: str, payload: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(payload, indent=1, default=str))
    print(f"wrote benchmarks/results/{name}.json")


# =============================================================================== workload


def gen_orders(n: int, seed: int, agents: list[str], dup_frac=0.10, invalid_frac=0.05,
               chunk: int | None = None) -> list[dict]:
    """Seeded order stream: valid buys/sells, exact duplicate retries, invalid requests.
    With `chunk`, order i is decided on bar i // chunk (a new hourly bar arrives every `chunk` orders),
    so working orders fill and close as in a real session instead of accumulating forever."""
    rng = random.Random(seed)
    out: list[dict] = []
    for i in range(n):
        u = rng.random()
        if out and u < dup_frac:
            out.append(dict(rng.choice(out)))
            continue
        o = {"client_order_id": f"s{seed}-o{i}", "agent_id": rng.choice(agents), "symbol": rng.choice(SYMS),
             "side": "BUY" if rng.random() < 0.7 else "SELL", "qty": rng.randint(1, 20),
             "signal_bar_ts": (T0 + timedelta(hours=i // chunk if chunk else 0)).isoformat()}
        if u < dup_frac + invalid_frac:
            if rng.random() < 0.5:
                o["qty"] = 0  # rejected by R6_INVALID_QTY
            else:
                o["symbol"] = "ZZZZ"  # rejected by R6_UNKNOWN_SYMBOL
        out.append(o)
    return out


def seeded_state(cash_usd: int, agents: list[str]) -> tuple[PortfolioState, EngineConfig]:
    cfg = EngineConfig()
    s = PortfolioState()
    process(s, OpenPortfolio(initial_cash=cash_usd * MICROS), cfg)
    for a in agents:
        process(s, SetAllocation(agent_id=a, capital=cash_usd * MICROS // (len(agents) + 1)), cfg)
    for sym in SYMS:
        m = PX[sym] * MICROS
        process(s, ApplyBar(bar=Bar(symbol=sym, ts=T0, open=m, high=m, low=m, close=m, volume=10**8)), cfg)
    return s, cfg


# =============================================================================== engine


def bench_engine() -> dict:
    agents = ["momentum", "mean_reversion", "volatility"]
    res: dict = {"environment": environment()}

    # A. pure risk-check latency on a fixed state (no mutation)
    s, cfg = seeded_state(10_000, agents)
    reqs = [OrderRequest.model_validate(o) for o in gen_orders(10_000, 1, agents)]
    reps = []
    for r in range(WARMUP + REPS):
        lat = []
        for q in reqs:
            t = time.perf_counter_ns()
            evaluate_order(s, q, cfg)
            lat.append((time.perf_counter_ns() - t) / 1000)
        if r >= WARMUP:
            reps.append(pct(lat))
    res["risk_check_latency_us"] = {"workload": "10,000 seeded proposals (10% duplicates, 5% invalid), fixed state",
                                    "reps": reps, "summary": summarize_reps(reps)}

    # Fixed positions; only the number of working buys grows. This isolates the
    # removed order scan from position marking, which is still O(positions).
    scaling = []
    for count in (0, 100, 1_000, 10_000):
        state, cfg = seeded_state(1_000_000_000, agents)
        for i in range(count):
            process(state, SubmitOrder(order=OrderRequest.model_validate({
                "client_order_id": f"working-{i}", "agent_id": agents[i % len(agents)],
                "symbol": "SPY", "side": "BUY", "qty": 1, "signal_bar_ts": T0.isoformat()})), cfg)
        passes = []
        for r in range(WARMUP + REPS):
            lat = []
            for q in reqs:
                t = time.perf_counter_ns()
                evaluate_order(state, q, cfg)
                lat.append((time.perf_counter_ns() - t) / 1000)
            if r >= WARMUP:
                passes.append(pct(lat))
        scaling.append({"open_orders": len(state.orders), "summary": summarize_reps(passes)})
    res["risk_check_open_order_scaling"] = scaling

    # B. full command latency: risk + event append + hash chain, state evolving, duplicates included
    chunked = [OrderRequest.model_validate(o) for o in gen_orders(10_000, 1, agents, chunk=CHUNK)]
    reps = []
    for r in range(WARMUP + REPS):
        s, cfg = seeded_state(1_000_000_000, agents)
        cmds: list = []
        for i, q in enumerate(chunked):
            if i and i % CHUNK == 0:
                cmds.extend(bar_cmds(i // CHUNK))
            cmds.append(SubmitOrder(order=q))
        lat, bar_lat = [], []
        t_all = time.perf_counter()
        for c in cmds:
            t = time.perf_counter_ns()
            process(s, c, cfg)
            (bar_lat if c.kind == "apply_bar" else lat).append((time.perf_counter_ns() - t) / 1000)
        el = time.perf_counter() - t_all
        if r >= WARMUP:
            reps.append({**pct(lat), "commands_per_s": len(cmds) / el, "bar_p50_us": float(np.median(bar_lat))})
    res["command_latency_us"] = {"workload": f"10,000 orders through process() incl. SHA-256 event chaining; "
                                             f"a 5-symbol bar every {CHUNK} orders fills working orders",
                                 "reps": reps, "summary": summarize_reps(reps)}

    # C. replay speed: backtests over 10k / full synthetic / 131k Binance bars
    t0 = time.perf_counter()
    syn, sm = load_snapshot(ROOT / "data/fixtures/synthetic_1h")
    load_syn = time.perf_counter() - t0
    datasets = [("synthetic_10k_bars", syn, 10_000), ("synthetic_34k_bars", syn, None)]
    bpath = BINANCE_SNAPSHOT
    if bpath.exists():
        t0 = time.perf_counter()
        bt, bm = load_snapshot(bpath)
        res["binance_snapshot"] = {k: bm[k] for k in ("dataset_id", "content_sha256", "unit_version")}
        datasets.append(("binance_btcusdt_131k_bars", bt, None))
        res["binance_load_s"] = time.perf_counter() - t0
    res["synthetic_load_s"] = load_syn
    replay_res = {}
    for name, tab, limit in datasets:
        p = SnapshotProvider(tab)
        syms = p.symbols()
        ts_sorted = p.t.ts
        end = int(ts_sorted[limit]) if limit else int(ts_sorted[-1]) + 1
        start = int(ts_sorted[0])
        n_bars = int(np.sum((ts_sorted >= start) & (ts_sorted < end)))
        spec = StrategySpec(strategy_id="bench", agent_id="bench", family="sma_crossover",
                            params={"fast": 20, "slow": 100}, universe=syms)
        reps = []
        for r in range(1 + 3):
            t = time.perf_counter()
            out = run_backtest(p, [Slot(spec, 9_000_000 * MICROS)], start, end, initial_cash=10_000_000 * MICROS)
            el = time.perf_counter() - t
            if r >= 1:
                reps.append({"seconds": el, "bars_per_s": n_bars / el, "events_per_s": out.events / el,
                             "events": out.events, "fills": out.metrics.n_fills})
        # event-log replay (what recovery does), no strategy work
        evs = _events_for(p, spec, start, end)
        rr = []
        for _ in range(3):
            t = time.perf_counter()
            replay(evs)
            rr.append(len(evs) / (time.perf_counter() - t))
        replay_res[name] = {"bars": n_bars, "symbols": len(syms), "backtest": summarize_reps(reps),
                            "event_log_replay_events_per_s": statistics.median(rr), "event_log_events": len(evs)}
    res["replay"] = replay_res
    save("engine", res)
    return res


def _events_for(p: SnapshotProvider, spec: StrategySpec, start: int, end: int,
                boundaries: list[int] | None = None) -> list[dict]:
    """Produce a realistic event log by running the engine (captures events as the worker would store them)."""
    import quantswarm.backtester as bt
    captured: list[dict] = []
    orig = bt.process

    def spy(s, c, cfg):
        evs, resp = orig(s, c, cfg)
        captured.extend(evs)
        if boundaries is not None and evs:
            boundaries.append(len(captured))
        return evs, resp
    bt.process = spy
    try:
        run_backtest(p, [Slot(spec, 9_000_000 * MICROS)], start, end, initial_cash=10_000_000 * MICROS)
    finally:
        bt.process = orig
    return captured


# =============================================================================== db / api


def compose(*args: str, check=True, capture=False):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=check, text=True,
                          capture_output=capture)


def start_bench_worker(portfolio: str, batch: int) -> str:
    name = f"qsbench-{portfolio}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    compose("run", "-d", "--rm", "--name", name, "worker", "python", "-m", "quantswarm.execution.worker",
            "--portfolio", portfolio, "--batch-size", str(batch), capture=True)
    return name


def wait_recovered(db, portfolio: str, after: datetime, timeout=60) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = db.execute("SELECT value FROM telemetry WHERE metric = 'recovery_seconds' AND labels->>'portfolio_id' = %s "
                       "AND ts > %s ORDER BY ts DESC LIMIT 1", (portfolio, after)).fetchone()
        if r:
            return r[0]
        time.sleep(0.02)
    raise TimeoutError("worker did not recover")


async def drive(api: str, portfolio: str, orders: list[dict], concurrency: int) -> dict:
    """Send orders with `concurrency` in flight. Every CHUNK orders a new bar is published (not timed
    as an order) so working orders fill, as they would in a live session.

    Uses aiohttp: an earlier httpx-based client capped out near 250 req/s at 50 connections and
    was measuring itself, not the system (see docs/PERFORMANCE.md)."""
    import aiohttp
    from quantswarm.api.client import api_headers
    lat: list[float] = []
    statuses: dict[str, int] = {}
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=concurrency + 2),
                                     timeout=aiohttp.ClientTimeout(total=60), headers=api_headers()) as c:
        async def one(o):
            async with sem:
                t = time.perf_counter()
                async with c.post(f"{api}/v1/orders?portfolio_id={portfolio}", json=o) as r:
                    body = await r.json()
                lat.append((time.perf_counter() - t) * 1000)
                key = str(r.status) if r.status != 200 else body.get("status", "?")
                statuses[key] = statuses.get(key, 0) + 1
        t0 = time.perf_counter()
        for k in range(0, len(orders), CHUNK):
            if k:
                async with c.post(f"{api}/v1/bars?portfolio_id={portfolio}",
                                  json={"dataset_id": "bench", "bars": bar_json(k // CHUNK)}) as r:
                    await r.read()
            await asyncio.gather(*(one(o) for o in orders[k:k + CHUNK]))
        el = time.perf_counter() - t0
    return {"seconds": el, "orders_per_s": len(orders) / el, "latency_ms": pct(lat), "statuses": statuses,
            "bars_published": (len(orders) - 1) // CHUNK}


def bench_db(n_orders: int, levels: list[int], reps: int, batches: list[int]) -> dict:
    import httpx
    import psycopg
    from quantswarm.api.client import api_headers
    from quantswarm.execution.db import database_url
    api = "http://localhost:8000"
    compose("up", "-d", "db", "api")
    for _ in range(100):
        try:
            if httpx.get(api + "/v1/health", timeout=2, headers=api_headers()).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    compose("stop", "worker", check=False)
    db = psycopg.connect(database_url(), autocommit=True, connect_timeout=10)
    agents = AGENTS_DB
    out: dict = {"environment": environment(), "limits": {"db": "2 vCPU / 2 GiB", "api": "1 vCPU / 512 MiB",
                                                          "worker": "1 vCPU / 512 MiB",
                                                          "load client": "1 vCPU / 512 MiB (in compose network)"},
                 "workload": f"{n_orders} orders per run, 10% exact duplicate retries, 5% invalid, 5 agents, "
                             "synchronous HTTP (response after the worker commits)", "runs": []}
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    for batch in batches:
        for c in levels:
            for r in range(reps + 1):  # first run = warm-up
                pid = f"b{stamp}-bs{batch}-c{c}-r{r}"
                after = datetime.now(timezone.utc)
                name = start_bench_worker(pid, batch)
                wait_recovered(db, pid, after)
                h = httpx.Client(base_url=api, timeout=30, headers=api_headers())
                h.post(f"/v1/admin/portfolio?portfolio_id={pid}", json={"initial_cash_usd": "1000000000"})
                for a in agents:
                    h.post(f"/v1/admin/allocations?portfolio_id={pid}", json={"agent_id": a, "capital_usd": "150000000"},
                           headers={"Idempotency-Key": a})
                h.post(f"/v1/bars?portfolio_id={pid}", json={"dataset_id": "bench", "bars": bar_json(0)})
                cp = compose("run", "--rm", "--no-deps", "bench", "python", "benchmarks/run_benchmarks.py", "drive",
                             "--portfolio", pid, "--orders", str(n_orders), "--concurrency", str(c),
                             "--seed", str(100 + r), capture=True)
                res = json.loads(next(l for l in cp.stdout.splitlines() if l.startswith("RESULT "))[7:])
                v = h.get(f"/v1/verify?portfolio_id={pid}").json()
                res.update({"portfolio": pid, "batch_size": batch, "concurrency": c, "rep": r, "warmup": r == 0,
                            "verify_match": v["match"], "ledger_events": v["events"]})
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                if not v["match"] or set(res["statuses"]) - {"accepted", "rejected"}:
                    raise RuntimeError(f"benchmark run failed verification/completion: {res}")
                print(f"batch={batch:3} c={c:3} rep={r} {res['orders_per_s']:8.1f} orders/s  "
                      f"p50={res['latency_ms']['p50']:.1f}ms p99={res['latency_ms']['p99']:.1f}ms verify={v['match']}")
                out["runs"].append(res)
    save("db_throughput", out)
    return out


# =============================================================================== recovery


def bench_recovery(sizes: list[int], reps: int) -> dict:
    import psycopg
    from psycopg.types.json import Jsonb

    from quantswarm.execution.worker import Worker, public_state
    from quantswarm.execution.db import database_url
    compose("up", "-d", "db")
    compose("stop", "worker", check=False)
    url = database_url()
    db = psycopg.connect(url, autocommit=True, connect_timeout=10)
    bpath = BINANCE_SNAPSHOT
    src, source_manifest = load_snapshot(bpath) if bpath.exists() else load_snapshot(ROOT / "data/fixtures/synthetic_1h")
    p = SnapshotProvider(src)
    spec = StrategySpec(strategy_id="rec", agent_id="rec", family="sma_crossover", params={"fast": 5, "slow": 20},
                        universe=p.symbols())
    boundaries: list[int] = []
    all_events = _events_for(p, spec, int(p.t.ts[0]), int(p.t.ts[-1]) + 1, boundaries)
    out: dict = {"environment": environment(), "runs": [], "source_events": len(all_events),
                 "checkpoint_interval": 10_000, "reps": reps, "requested_sizes": sizes,
                 "unavailable_sizes": [n for n in sizes if n > len(all_events)],
                 "dataset": "binance_btcusdt_131k_bars" if bpath.exists() else "synthetic_34k_bars",
                 "dataset_id": source_manifest["dataset_id"], "content_sha256": source_manifest["content_sha256"],
                 "unit_version": source_manifest.get("unit_version", "shares-v1")}
    sizes = [n for n in sizes if n <= len(all_events)]
    if not sizes:
        raise RuntimeError("no requested recovery sizes fit the dataset")
    if out["unavailable_sizes"]:
        print(f"dataset too small for sizes {out['unavailable_sizes']}; download Binance data to measure them")
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    for requested in sizes:
        # A fill/close pair can leave a transient zero-remaining order between
        # events. Durable recovery workloads must end at complete commands.
        n = boundaries[bisect_right(boundaries, requested) - 1]
        evs = all_events[:n]
        state = replay(evs)
        pid = f"rec{stamp}-{n}"
        cseq = db.execute("INSERT INTO commands (command_id, portfolio_id, kind, payload, payload_hash, status) "
                          "VALUES (%s, %s, 'bench', '{}', '', 'done') RETURNING seq", (f"bench:{pid}", pid)).fetchone()[0]
        with db.cursor().copy("COPY ledger_events (portfolio_id, seq, command_seq, type, event, hash) FROM STDIN") as cp:
            for e in evs:
                cp.write_row((pid, e["seq"], cseq, e["type"], json.dumps(e), e["hash"]))
        db.execute("INSERT INTO portfolio_state (portfolio_id, seq, head_hash, digest, state, equity) VALUES "
                   "(%s,%s,%s,%s,%s,%s)", (pid, state.seq, state.head_hash, state.digest(), Jsonb(public_state(state)),
                                           state.equity()))
        for mode in ("full", "checkpoint"):
            cp_seq = n // out["checkpoint_interval"] * out["checkpoint_interval"] if mode == "checkpoint" else 0
            if cp_seq:
                cp_seq = boundaries[bisect_right(boundaries, cp_seq) - 1]
            if cp_seq:
                writer = Worker(db, pid)
                writer.state = replay(evs[:cp_seq])
                writer.write_checkpoint()
            inproc = []
            for r in range(1 + reps):
                w = Worker(db, pid)
                info = w.recover()
                if w.state.snapshot() != state.snapshot() or info["events"] != n - cp_seq:
                    raise RuntimeError("recovery benchmark differs from independent full replay")
                if r:
                    inproc.append({"load_s": info["load_s"], "replay_s": info["replay_s"], "total_s": info["total_s"]})
            cold = []
            for r in range(reps):
                after = datetime.now(timezone.utc)
                t0 = time.perf_counter()
                name = start_bench_worker(pid, 64)
                rec_s = wait_recovered(db, pid, after, timeout=300)
                cold.append({"container_start_to_ready_s": time.perf_counter() - t0, "worker_reported_recovery_s": rec_s})
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            row = {"events": n, "requested_events": requested, "mode": mode, "checkpoint_seq": cp_seq, "replayed_events": n - cp_seq,
                   "in_process": summarize_reps(inproc), "container_restart": summarize_reps(cold), "verify_match": True}
            print(f"{n:7} events ({mode}, suffix={n-cp_seq}): recovery {row['in_process']['total_s']['median']:.3f}s, "
                  f"restart-to-ready {row['container_restart']['container_start_to_ready_s']['median']:.2f}s")
            out["runs"].append(row)
    save("recovery", out)
    return out


def drive_cli(api: str, pid: str, n: int, c: int, seed: int) -> None:
    """Load client entry point; runs inside the compose network (see bench_db) so the
    measurement does not include host-to-VM port forwarding."""
    orders = gen_orders(n, seed=seed, agents=AGENTS_DB, chunk=CHUNK)
    for o in orders:
        o["client_order_id"] = f"{pid}-{o['client_order_id']}"
    print("RESULT " + json.dumps(asyncio.run(drive(api, pid, orders, c))))


if __name__ == "__main__":
    from quantswarm.config import load_env
    load_env()
    if len(sys.argv) > 1 and sys.argv[1] == "drive":
        dp = argparse.ArgumentParser()
        dp.add_argument("cmd")
        dp.add_argument("--api", default="http://api:8000")
        dp.add_argument("--portfolio", required=True)
        dp.add_argument("--orders", type=int, required=True)
        dp.add_argument("--concurrency", type=int, required=True)
        dp.add_argument("--seed", type=int, required=True)
        d = dp.parse_args()
        drive_cli(d.api, d.portfolio, d.orders, d.concurrency, d.seed)
        sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["engine", "db", "recovery", "all"])
    ap.add_argument("--orders", type=int, default=10_000)
    ap.add_argument("--levels", type=int, nargs="+", default=[1, 10, 50])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--batches", type=int, nargs="+", default=[64])
    ap.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 100_000, 105_000])
    a = ap.parse_args()
    if a.which in ("engine", "all"):
        r = bench_engine()
        print(json.dumps({k: (v.get("summary", v) if isinstance(v, dict) else v) for k, v in r.items() if k != "environment"}, indent=1, default=str)[:4000])
    if a.which in ("db", "all"):
        bench_db(a.orders, a.levels, a.reps, a.batches)
    if a.which in ("recovery", "all"):
        bench_recovery(a.sizes, a.reps)
