"""Thin OpenAI wrapper with an offline fallback.

Every LLM call returns JSON that is validated by the caller. If no API key is
configured (or the call fails), callers fall back to recorded fixtures so the
deterministic pipeline - and the demo - still run end to end.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from hellabandz.config import load_env

load_env()
log = logging.getLogger("llm")
MODEL = os.environ.get("QS_OPENAI_MODEL", "gpt-4.1-mini")
# reasoning-model families reject a custom temperature
_NO_TEMPERATURE = ("o1", "o3", "o4", "gpt-5")


def available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def chat_json(system: str, user: str, temperature: float = 0.4, timeout: float = 60) -> tuple[dict, dict]:
    """Return (parsed_json, meta). Raises on failure; callers decide on fallback."""
    from openai import OpenAI

    client = OpenAI(timeout=timeout)
    t0 = time.perf_counter()
    kwargs: dict[str, Any] = {} if MODEL.startswith(_NO_TEMPERATURE) else {"temperature": temperature}
    r = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **kwargs,
    )
    text = r.choices[0].message.content or "{}"
    meta: dict[str, Any] = {"model": r.model, "latency_s": round(time.perf_counter() - t0, 3),
                            "prompt_tokens": r.usage.prompt_tokens if r.usage else None,
                            "completion_tokens": r.usage.completion_tokens if r.usage else None}
    return json.loads(text), meta
