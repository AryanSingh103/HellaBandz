"""Database helpers shared by the API and the execution worker."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import TypeAdapter

from hellabandz.contracts import Command
from hellabandz.ledger import canonical, sha256_hex

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
ACCOUNTS_SCHEMA = (Path(__file__).parent / "accounts.sql").read_text()
COMMAND_ADAPTER: TypeAdapter = TypeAdapter(Command)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://quantswarm:quantswarm@localhost:5432/quantswarm")


def encode_command(cmd) -> tuple[str, dict, str]:
    """-> (kind, json payload, payload hash)."""
    payload = cmd.model_dump(mode="json")
    return cmd.kind, payload, sha256_hex(canonical(payload))


def decode_command(payload: dict):
    return COMMAND_ADAPTER.validate_python(payload)


def apply_schema(conn) -> None:
    # serialize concurrent first-start schema creation across processes
    conn.execute("SELECT pg_advisory_lock(424242)")
    try:
        conn.execute(SCHEMA)
        conn.execute(ACCOUNTS_SCHEMA)
    finally:
        conn.execute("SELECT pg_advisory_unlock(424242)")
