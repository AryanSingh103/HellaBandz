"""Gemini JSON calls, with explicit OpenAI compatibility and offline fallback.

Every LLM call returns JSON that is validated by the caller. If no API key is
configured (or the call fails), callers fall back to recorded fixtures so the
deterministic pipeline - and the demo - still run end to end.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import threading
from typing import Any

from hellabandz.config import load_env

load_env()
log = logging.getLogger("llm")
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_gemini_retry_at = 0.0
_circuit_lock = threading.Lock()
# reasoning-model families reject a custom temperature
_NO_TEMPERATURE = ("o1", "o3", "o4", "gpt-5")


def selected_provider(provider: str | None = None) -> str:
    selected = (provider or os.getenv("QS_LLM_PROVIDER", "gemini")).strip().lower()
    if selected not in ("gemini", "openai", "offline"):
        raise ValueError("QS_LLM_PROVIDER must be gemini, openai, or offline")
    return selected


def model_name(provider: str | None = None) -> str | None:
    selected = selected_provider(provider)
    if selected == "offline":
        return None
    return (os.getenv("QS_GEMINI_MODEL") or GEMINI_MODEL) if selected == "gemini" else (
        os.getenv("QS_OPENAI_MODEL") or "gpt-4.1-mini")


def api_key(provider: str | None = None) -> str | None:
    selected = selected_provider(provider)
    if selected == "gemini":
        return os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip() or None
    if selected == "openai":
        return os.getenv("OPENAI_API_KEY", "").strip() or None
    return None


def available(provider: str | None = None) -> bool:
    selected = selected_provider(provider)
    return bool(api_key(selected) or (selected == "gemini" and fallback_enabled() and api_key("openai")))


def fallback_enabled() -> bool:
    return os.getenv("QS_OPENAI_FALLBACK", "1").strip().lower() not in ("0", "false", "off", "no")


def _failure_label(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" HTTP {status}" if isinstance(status, int) else "")


def _call_json(system: str, user: str, temperature: float, timeout: float,
               max_retries: int, provider: str) -> tuple[dict, dict]:
    """Return (parsed_json, meta). Raises on failure; callers decide on fallback."""
    from openai import OpenAI

    selected = provider
    key = api_key(selected)
    if not key:
        required = "GEMINI_API_KEY or GOOGLE_API_KEY" if selected == "gemini" else "OPENAI_API_KEY"
        raise ValueError("LLM calls disabled by offline selection" if selected == "offline" else f"{selected} requires {required}")
    model = model_name(selected)
    client = OpenAI(api_key=key, base_url=GEMINI_BASE_URL if selected == "gemini" else "https://api.openai.com/v1",
                    timeout=timeout, max_retries=max_retries)
    t0 = time.perf_counter()
    kwargs: dict[str, Any] = {} if selected == "openai" and model.startswith(_NO_TEMPERATURE) else {"temperature": temperature}
    r = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **kwargs,
    )
    text = r.choices[0].message.content
    parsed = json.loads(text or "")
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object")
    meta: dict[str, Any] = {"provider": selected, "model": r.model, "latency_s": round(time.perf_counter() - t0, 3),
                            "prompt_tokens": r.usage.prompt_tokens if r.usage else None,
                            "completion_tokens": r.usage.completion_tokens if r.usage else None}
    return parsed, meta


def chat_json(system: str, user: str, temperature: float = 0.4, timeout: float = 60,
              max_retries: int = 2, *, provider: str | None = None) -> tuple[dict, dict]:
    """Gemini primary, bounded OpenAI failover; one shared caller deadline.

    A failed Gemini request opens a short circuit so subsequent run calls use
    the backup immediately. Errors never include credentials or provider bodies.
    """
    global _gemini_retry_at
    selected = selected_provider(provider)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if selected != "gemini":
        return _call_json(system, user, temperature, timeout, max_retries, selected)
    started = time.monotonic()
    deadline = started + timeout
    primary_timeout = max(.1, float(os.getenv("QS_GEMINI_TIMEOUT_S", "8")))
    cooldown = max(0., float(os.getenv("QS_GEMINI_COOLDOWN_S", "60")))
    if fallback_enabled() and api_key("openai"):
        primary_timeout = min(primary_timeout, timeout / 2)
    with _circuit_lock:
        circuit_open = started < _gemini_retry_at
    reason = "gemini_circuit_open"
    if not circuit_open:
        try:
            result, meta = _call_json(system, user, temperature, min(timeout, primary_timeout), 0, "gemini")
            with _circuit_lock:
                if _gemini_retry_at <= started:
                    _gemini_retry_at = 0.0
            return result, meta
        except Exception as exc:
            reason = _failure_label(exc)
            with _circuit_lock:
                _gemini_retry_at = time.monotonic() + cooldown
    if not fallback_enabled() or not api_key("openai"):
        raise RuntimeError(f"Gemini unavailable ({reason}); OpenAI backup disabled or unconfigured") from None
    remaining = deadline - time.monotonic()
    if remaining <= .05:
        raise TimeoutError("Gemini failed; caller deadline exhausted before OpenAI backup") from None
    try:
        result, meta = _call_json(system, user, temperature, remaining, 0, "openai")
    except Exception as exc:
        raise RuntimeError(f"Gemini unavailable ({reason}); OpenAI backup failed ({_failure_label(exc)})") from None
    return result, {**meta, "primary_provider": "gemini", "fallback_from": "gemini", "fallback_reason": reason,
                    "latency_s": round(time.monotonic() - started, 3)}
