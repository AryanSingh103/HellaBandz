"""Spin up an isolated database + real worker/API processes per test module."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("QS_TEST_ADMIN_URL", "postgresql://quantswarm:quantswarm@127.0.0.1:5432/postgres")


@pytest.fixture(autouse=True)
def isolated_test_auth(monkeypatch):
    # Disposable test stacks explicitly opt out; auth tests override this to exercise fail-closed access.
    monkeypatch.setenv("QS_AUTH_REQUIRED", "0")


def _db_available() -> bool:
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=2):
            return True
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    def __init__(self, url: str):
        self.url = url
        self.worker: subprocess.Popen | None = None
        self.api: subprocess.Popen | None = None
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"

    def env(self, **extra: str) -> dict:
        return {**os.environ, "DATABASE_URL": self.url, "PYTHONUNBUFFERED": "1", **extra}

    def start_worker(self, **extra: str) -> subprocess.Popen:
        self.worker = subprocess.Popen([sys.executable, "-m", "quantswarm.execution.worker"], cwd=ROOT,
                                       env=self.env(**extra), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return self.worker

    def wait_worker_ready(self, timeout: float = 20) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with psycopg.connect(self.url, autocommit=True) as c:
                n = c.execute("SELECT count(*) FROM telemetry WHERE metric = 'recovery_seconds'").fetchone()[0]
            if n >= getattr(self, "_recoveries", 0) + 1:
                self._recoveries = n
                return
            if self.worker and self.worker.poll() is not None:
                raise RuntimeError(f"worker exited: {self.worker.stdout.read().decode()[-2000:]}")
            time.sleep(0.05)
        raise TimeoutError("worker did not become ready")

    def kill_worker(self) -> None:
        if self.worker and self.worker.poll() is None:
            self.worker.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
            self.worker.wait()

    def start_api(self, **extra: str) -> None:
        # psycopg async connections require a selector loop on Windows. Uvicorn
        # defaults to ProactorEventLoop there; Docker/Linux keeps its normal loop.
        loop_args = ["--loop", "asyncio:SelectorEventLoop"] if sys.platform == "win32" else []
        self.api = subprocess.Popen([sys.executable, "-m", "uvicorn", "quantswarm.api.app:app", "--port",
                                     str(self.port), "--log-level", "warning", *loop_args], cwd=ROOT, env=self.env(**extra),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(200):
            try:
                if httpx.get(self.base + "/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise TimeoutError("api did not start")

    def sql(self, query: str, args: tuple = ()) -> list[tuple]:
        with psycopg.connect(self.url, autocommit=True) as c:
            cur = c.execute(query, args)
            return cur.fetchall() if cur.description else []

    def stop(self) -> None:
        self.kill_worker()
        if self.api and self.api.poll() is None:
            self.api.terminate()
            self.api.wait()


@pytest.fixture()
def stack():
    if not _db_available():
        pytest.skip("Postgres not reachable; run `docker compose up -d db`")
    name = "qs_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    st = Stack(url)
    from quantswarm.execution.db import apply_schema
    with psycopg.connect(url, autocommit=True) as c:
        apply_schema(c)
    try:
        yield st
    finally:
        st.stop()
        with psycopg.connect(ADMIN_URL, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def enqueue(st: Stack, cmd, command_id: str, portfolio_id: str = "main") -> int:
    """Insert a command exactly like the API does (used where HTTP is not the subject under test)."""
    from psycopg.types.json import Jsonb

    from quantswarm.execution.db import encode_command
    kind, payload, h = encode_command(cmd)
    rows = st.sql("INSERT INTO commands (command_id, portfolio_id, kind, payload, payload_hash) VALUES "
                  "(%s,%s,%s,%s,%s) ON CONFLICT (command_id) DO NOTHING RETURNING seq",
                  (command_id, portfolio_id, kind, Jsonb(payload), h))
    st.sql("SELECT pg_notify('cmd_new', '')")
    return rows[0][0] if rows else -1


def expected_from_command_log(st: Stack):
    """Re-run the durable command log through the pure engine, in processing order."""
    from quantswarm.execution.db import decode_command
    from quantswarm.ledger import EngineConfig, PortfolioState, process
    s = PortfolioState()
    for (payload,) in st.sql("SELECT payload FROM commands WHERE status = 'done' ORDER BY processed_at, seq"):
        process(s, decode_command(payload), EngineConfig())
    return s


def wait_until(pred, timeout: float = 30, every: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(every)
    raise TimeoutError("condition not met")
