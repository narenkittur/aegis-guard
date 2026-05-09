"""
backend/app/config.py — Aegis-Guard Settings
=============================================
All runtime configuration is loaded from the ``.env`` file (or from real
environment variables) via ``pydantic-settings``.  Nothing is hard-coded.

Usage
-----
Import ``get_settings()`` wherever you need config.  The result is cached so
the file is only parsed once.

.. code-block:: python

    from backend.app.config import get_settings

    settings = get_settings()
    print(settings.llm_api_key)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parents[3] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── API / LLM ─────────────────────────────────────────────────────────────
    llm_provider: str = Field(
        default="openai",
        description="Which LLM provider to call: 'openai' | 'gemini' | 'anthropic'.",
    )
    llm_api_key: str = Field(
        default="",
        description="API key for the chosen LLM provider.  Set in .env.",
    )
    llm_model: str = Field(
        default="gpt-4o",
        description="Model identifier forwarded to the provider.",
    )

    # ── Vault ─────────────────────────────────────────────────────────────────
    db_path: Path = Field(
        default=Path(__file__).parent / "vault.db",
        description="Absolute path to the SQLite vault file.",
    )

    # ── Security (Phase 2) ────────────────────────────────────────────────────
    secret_key: str = Field(
        default="",
        description="32-byte hex key for AES-256-GCM vault encryption (Phase 2).",
    )

    # ── Server ────────────────────────────────────────────────────────────────
    cors_origins: list[str] = Field(
        default=["http://localhost:8501"],  # Streamlit default port
        description="Allowed CORS origins.",
    )
    api_rate_limit: str = Field(
        default="60/minute",
        description="slowapi rate limit string for all routes.",
    )

    # ── NLP ───────────────────────────────────────────────────────────────────
    spacy_model: str = Field(
        default="en_core_web_lg",
        description="spaCy model to load.  Must be pre-downloaded.",
    )
    presidio_min_score: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Minimum Presidio confidence to act on a detected entity.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Returns
    -------
    Settings
        Fully-populated settings instance.
    """
    return Settings()
