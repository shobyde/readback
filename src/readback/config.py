"""Configuration for Readback.

Every setting comes from the environment; nothing is required except that the
database path resolves to a writable location.  ``load_settings`` is the only
public entry point other modules use.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_DB_PATH = "./readback.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5057
DEFAULT_LLM_BASE_URL = "http://localhost:11434/v1"
DEFAULT_LLM_API_KEY = ""
DEFAULT_LLM_MODEL = "llama3.1:8b"
DEFAULT_LLM_TIMEOUT = 120.0
DEFAULT_PASS_THRESHOLD = 0.8
DEFAULT_PARENT_PIN = ""


@dataclass(frozen=True)
class Settings:
    """Fully resolved application settings."""

    db_path: Path
    host: str
    port: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    pass_threshold: float
    parent_pin: str
    secret_key: str
    llm_timeout: float = DEFAULT_LLM_TIMEOUT


def _text(source: Mapping[str, str], name: str, default: str) -> str:
    """Return a stripped env value, falling back to ``default`` when unset/blank."""
    value = source.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (or ``os.environ``).

    Raises ``ValueError`` when ``READBACK_PORT`` is not an integer or when
    ``READBACK_PASS_THRESHOLD`` is not a number in ``(0.0, 1.0]``.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    port_text = _text(source, "READBACK_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        raise ValueError(
            f"READBACK_PORT must be an integer, got {port_text!r}"
        ) from None

    threshold_text = _text(
        source, "READBACK_PASS_THRESHOLD", str(DEFAULT_PASS_THRESHOLD)
    )
    try:
        pass_threshold = float(threshold_text)
    except (TypeError, ValueError):
        raise ValueError(
            f"READBACK_PASS_THRESHOLD must be a number between 0 and 1, "
            f"got {threshold_text!r}"
        ) from None
    if not 0.0 < pass_threshold <= 1.0:
        raise ValueError(
            "READBACK_PASS_THRESHOLD must be greater than 0 and at most 1, "
            f"got {pass_threshold!r}"
        )

    secret_key = _text(source, "READBACK_SECRET_KEY", "") or secrets.token_hex(32)

    timeout_text = _text(
        source, "READBACK_LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT)
    )
    try:
        llm_timeout = float(timeout_text)
    except (TypeError, ValueError):
        raise ValueError(
            f"READBACK_LLM_TIMEOUT must be a number of seconds, got {timeout_text!r}"
        ) from None
    if llm_timeout <= 0:
        raise ValueError(
            f"READBACK_LLM_TIMEOUT must be greater than 0, got {llm_timeout!r}"
        )

    return Settings(
        db_path=Path(_text(source, "READBACK_DB", DEFAULT_DB_PATH)),
        host=_text(source, "READBACK_HOST", DEFAULT_HOST),
        port=port,
        llm_base_url=_text(source, "READBACK_LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        llm_api_key=_text(source, "READBACK_LLM_API_KEY", DEFAULT_LLM_API_KEY),
        llm_model=_text(source, "READBACK_LLM_MODEL", DEFAULT_LLM_MODEL),
        pass_threshold=pass_threshold,
        parent_pin=_text(source, "READBACK_PARENT_PIN", DEFAULT_PARENT_PIN),
        secret_key=secret_key,
        llm_timeout=llm_timeout,
    )


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_LLM_BASE_URL",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_LLM_TIMEOUT",
    "DEFAULT_PASS_THRESHOLD",
    "Settings",
    "load_settings",
]
