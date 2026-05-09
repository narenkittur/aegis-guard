"""
backend/app/main.py — Aegis-Guard FastAPI Application
======================================================
Defines the ASGI app, lifespan hooks, and all API routes.

Routes
------
GET  /health                 Liveness probe — returns {"status": "shield-active"}.
POST /api/v1/scrub           De-identify a user prompt (calls scrubber.anonymize).
POST /api/v1/restore         Restore PII into an LLM response (calls scrubber.rehydrate).
DELETE /session              Wipe all vault mappings (admin / end-of-session).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from backend.app.config import get_settings
from backend.app.database import clear_session, initialize_db
import backend.app.scrubber as scrubber
from shared.models import (
    RehydrateRequest,
    RehydrateResponse,
    ScrubRequest,
    ScrubResponse,
    SessionClearResponse,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────

settings = get_settings()
limiter = Limiter(key_func=get_remote_address, default_limits=[settings.api_rate_limit])


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: initialise DB and warm Presidio engines.  Shutdown: log teardown."""
    logger.info("=== Aegis-Guard starting up ===")
    await initialize_db()
    scrubber.warm_engines()     # loads spaCy model once — avoids per-request penalty
    logger.info("=== Startup complete — API ready ===")
    yield
    logger.info("=== Aegis-Guard shutting down ===")


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Aegis-Guard API",
    description="Local-first PII redaction gateway with full round-trip de-identification.",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Broad localhost origins for local development (Streamlit :8501, browsers, curl).
_DEV_ORIGINS: list[str] = [
    "http://localhost",
    "http://localhost:8501",    # Streamlit default
    "http://localhost:3000",    # generic frontend dev server
    "http://127.0.0.1:8501",
    *settings.cors_origins,     # anything extra declared in .env
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(_DEV_ORIGINS)),  # deduplicated, order-preserving
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["Ops"])
async def health_check() -> JSONResponse:
    """Liveness probe.

    Returns
    -------
    JSONResponse
        ``{"status": "shield-active"}`` when the service is up.
    """
    return JSONResponse(content={"status": "shield-active"})


@app.post("/api/v1/scrub", response_model=ScrubResponse, tags=["Core"])
@limiter.limit(settings.api_rate_limit)
async def scrub(request: Request, body: ScrubRequest) -> ScrubResponse:
    """De-identify a user prompt.

    Detects PERSON, EMAIL_ADDRESS, and PHONE_NUMBER entities, replaces each
    with an opaque token (e.g. ``[PERSON_1]``), and persists the mapping to
    the vault so it can be reversed later.

    Parameters
    ----------
    request:
        FastAPI ``Request`` object (required by slowapi limiter).
    body:
        ``ScrubRequest`` containing the raw prompt text.

    Returns
    -------
    ScrubResponse
        Scrubbed text, token count, and entity types detected.

    Raises
    ------
    HTTPException
        500 if the scrubbing pipeline or vault raises an unexpected error.
    """
    try:
        scrubbed_text, token_count, entities_found = await scrubber.anonymize(body.text)
    except OSError as exc:
        logger.exception("spaCy model not found during scrub")
        raise HTTPException(status_code=500, detail=f"NLP model error: {exc}") from exc
    except Exception as exc:
        logger.exception("Unexpected error during scrub")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ScrubResponse(
        scrubbed_text=scrubbed_text,
        token_count=token_count,
        entities_found=entities_found,
    )


@app.post("/api/v1/restore", response_model=RehydrateResponse, tags=["Core"])
@limiter.limit(settings.api_rate_limit)
async def restore(request: Request, body: RehydrateRequest) -> RehydrateResponse:
    """Restore PII into an LLM response.

    Scans for ``[ENTITY_TYPE_N]`` tokens and swaps each one back to its
    original value from the vault.  Tokens not found in the vault are left
    in-place; the caller receives an ``tokens_unresolved`` count to surface
    a warning in the UI if needed.

    Parameters
    ----------
    request:
        FastAPI ``Request`` object (required by slowapi limiter).
    body:
        ``RehydrateRequest`` containing the tokenized LLM response.

    Returns
    -------
    RehydrateResponse
        Restored text, resolved count, and unresolved count.

    Raises
    ------
    HTTPException
        500 if the vault raises an unexpected error.
    """
    try:
        original_text, tokens_resolved, tokens_unresolved = await scrubber.rehydrate(body.text)
    except Exception as exc:
        logger.exception("Unexpected error during restore")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RehydrateResponse(
        original_text=original_text,
        tokens_resolved=tokens_resolved,
        tokens_unresolved=tokens_unresolved,
    )


@app.delete("/session", response_model=SessionClearResponse, tags=["Admin"])
async def end_session() -> SessionClearResponse:
    """Wipe all PII mappings from the vault.

    .. warning::
        Irreversible.  After this call, rehydration of any previously-scrubbed
        text will fail silently (tokens left in-place).

    Returns
    -------
    SessionClearResponse
        Number of mappings permanently removed.
    """
    deleted = await clear_session()
    return SessionClearResponse(deleted_count=deleted)
