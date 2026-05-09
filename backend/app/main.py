"""
backend/app/main.py — Aegis-Guard FastAPI Application
======================================================
Defines the ASGI app, lifespan hooks, and all API routes.

Routes
------
GET  /health           Liveness probe — checks vault + Presidio readiness.
POST /scrub            De-identify a user prompt.
POST /rehydrate        Restore PII into an LLM response.
DELETE /session        Wipe all vault mappings (admin / end-of-session).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from backend.app.config import get_settings
from backend.app.database import clear_session, get_original, initialize_db
from backend.app.scrubber import deidentify_text, rehydrate_text, warm_engines
from shared.models import (
    HealthResponse,
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
    """Startup: initialise DB and warm Presidio engines.  Shutdown: (future cleanup)."""
    logger.info("=== Aegis-Guard starting up ===")
    await initialize_db()
    warm_engines()          # loads spaCy model — takes ~8 s first call
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse, tags=["Ops"])
async def health_check() -> HealthResponse:
    """Liveness probe.  Returns vault + Presidio readiness status."""
    vault_ok = False
    try:
        # Lightweight check: try to resolve a token we know doesn't exist.
        await get_original("__health_check__")
        vault_ok = True
    except Exception:  # noqa: BLE001
        vault_ok = False

    from backend.app.scrubber import _ENGINES  # noqa: PLC0415
    presidio_ok = _ENGINES is not None

    return HealthResponse(vault_reachable=vault_ok, presidio_ready=presidio_ok)


@app.post("/scrub", response_model=ScrubResponse, tags=["Core"])
@limiter.limit(settings.api_rate_limit)
async def scrub(request: Request, body: ScrubRequest) -> ScrubResponse:
    """De-identify a user prompt.

    Detects PII, replaces with opaque tokens, persists mappings to the vault.
    """
    try:
        scrubbed_text, token_count, entities_found = await deidentify_text(body.text)
    except Exception as exc:
        logger.exception("Error during de-identification")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ScrubResponse(
        scrubbed_text=scrubbed_text,
        token_count=token_count,
        entities_found=entities_found,
    )


@app.post("/rehydrate", response_model=RehydrateResponse, tags=["Core"])
@limiter.limit(settings.api_rate_limit)
async def rehydrate(request: Request, body: RehydrateRequest) -> RehydrateResponse:
    """Restore PII into an LLM response.

    Scans for Aegis-Guard tokens and swaps each one back to its original value.
    """
    try:
        original_text, tokens_resolved, tokens_unresolved = await rehydrate_text(body.text)
    except Exception as exc:
        logger.exception("Error during rehydration")
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
    """
    deleted = await clear_session()
    return SessionClearResponse(deleted_count=deleted)
