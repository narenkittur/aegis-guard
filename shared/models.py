"""
shared/models.py — Aegis-Guard Pydantic Contracts
==================================================
Single source of truth for all request/response schemas.
Both the FastAPI backend and the Streamlit frontend import from here,
ensuring the API surface is always in sync.

Design notes
------------
* All models inherit from ``BaseModel`` (Pydantic v2).
* ``model_config = ConfigDict(frozen=True)`` makes instances hashable and
  prevents accidental mutation after construction.
* Keep this file dependency-free (only stdlib + pydantic) so the frontend
  can import it without dragging in FastAPI or aiosqlite.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# ── Scrub (de-identification) ─────────────────────────────────────────────────


class ScrubRequest(BaseModel):
    """Payload sent to ``POST /scrub``."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(
        ...,
        min_length=1,
        description="Raw user prompt that may contain PII.",
        examples=["Call Alice at alice@corp.com regarding SSN 123-45-6789."],
    )


class ScrubResponse(BaseModel):
    """Response from ``POST /scrub``."""

    model_config = ConfigDict(frozen=True)

    scrubbed_text: str = Field(
        ...,
        description="Prompt with all detected PII replaced by opaque tokens.",
        examples=["Call [PERSON_1] at [EMAIL_ADDRESS_2] regarding SSN [US_SSN_3]."],
    )
    token_count: int = Field(
        ...,
        ge=0,
        description="Number of distinct PII tokens inserted in this request.",
    )
    entities_found: list[str] = Field(
        default_factory=list,
        description="Ordered list of entity-type labels detected, e.g. ['PERSON', 'EMAIL_ADDRESS'].",
    )


# ── Rehydrate (re-identification) ────────────────────────────────────────────


class RehydrateRequest(BaseModel):
    """Payload sent to ``POST /rehydrate``."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(
        ...,
        min_length=1,
        description="LLM response text that may contain Aegis-Guard tokens.",
        examples=["Here is a summary for [PERSON_1] whose email is [EMAIL_ADDRESS_2]."],
    )


class RehydrateResponse(BaseModel):
    """Response from ``POST /rehydrate``."""

    model_config = ConfigDict(frozen=True)

    original_text: str = Field(
        ...,
        description="Text with all recognised tokens swapped back to original PII.",
        examples=["Here is a summary for Alice whose email is alice@corp.com."],
    )
    tokens_resolved: int = Field(
        ...,
        ge=0,
        description="Number of tokens successfully resolved from the vault.",
    )
    tokens_unresolved: int = Field(
        ...,
        ge=0,
        description=(
            "Number of tokens found in the text but absent from the vault "
            "(e.g. after a session clear). Unresolved tokens are left as-is."
        ),
    )


# ── Session management ────────────────────────────────────────────────────────


class SessionClearResponse(BaseModel):
    """Response from ``DELETE /session``."""

    model_config = ConfigDict(frozen=True)

    deleted_count: int = Field(
        ...,
        ge=0,
        description="Number of token→PII mappings permanently removed from the vault.",
    )
    message: str = Field(
        default="Session cleared. All PII mappings have been destroyed.",
        description="Human-readable confirmation message.",
    )


# ── Health check ──────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Response from ``GET /health``."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(default="ok")
    vault_reachable: bool = Field(
        ...,
        description="True if the SQLite vault responded to a test query.",
    )
    presidio_ready: bool = Field(
        ...,
        description="True if the Presidio AnalyzerEngine is loaded and warm.",
    )
