"""Database helpers shared by the API and the execution worker."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import TypeAdapter

from quantswarm.contracts import Command
from quantswarm.ledger import canonical, sha256_hex

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()
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
    finally:
        conn.execute("SELECT pg_advisory_unlock(424242)")
