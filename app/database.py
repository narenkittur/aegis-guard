"""
app/database.py — Aegis-Guard Local PII Vault
==============================================
Manages the SQLite-backed pii_vault: the reversible de-identification store
that maps opaque tokens ("[PERSON_1]") back to their original PII values.

Design notes
------------
* Uses ``aiosqlite`` so every DB call is non-blocking inside FastAPI's event
  loop.  Standard ``sqlite3`` would block the entire server thread.
* Every function opens **and closes** its own connection via ``async with``
  so no connection is held between requests (prevents SQLITE_BUSY locks).
* The ``original_value`` column stores data in plain-text in this prototype.
  Phase 2 will swap this for AES-256-GCM ciphertext (see encryption.py).
* ``clear_session()`` is intentionally destructive and should be guarded
  behind an admin-only route in production.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

# ── Configuration ────────────────────────────────────────────────────────────

# Vault lives next to this file; override via env var in production.
DB_PATH: Path = Path(__file__).parent / "vault.db"

logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pii_vault (
    id             INTEGER   PRIMARY KEY AUTOINCREMENT,
    token          TEXT      NOT NULL UNIQUE,          -- e.g. "[PERSON_1]"
    original_value TEXT      NOT NULL,                 -- e.g. "John Doe"
    entity_type    TEXT      NOT NULL,                 -- e.g. "PERSON"
    created_at     TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pii_vault_token
    ON pii_vault (token);
"""


# ── Public API ────────────────────────────────────────────────────────────────


async def initialize_db() -> None:
    """Create the pii_vault table (and its index) if they don't exist yet.

    Call this once from FastAPI's ``lifespan`` startup hook so the schema is
    guaranteed to exist before the first request arrives.

    Example
    -------
    .. code-block:: python

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await initialize_db()
            yield
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode = WAL;")  # concurrent reads
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute(_CREATE_TABLE_SQL)
        await conn.execute(_CREATE_INDEX_SQL)
        await conn.commit()

    logger.info("PII vault initialised at %s", DB_PATH)


async def store_mapping(token: str, original: str, entity: str) -> None:
    """Persist a new token → PII mapping in the vault.

    If *token* already exists (same text scrubbed twice in a session) the row
    is silently skipped via ``INSERT OR IGNORE`` so callers don't have to
    deduplicate first.

    Parameters
    ----------
    token:
        The opaque placeholder inserted into scrubbed text, e.g. ``"[EMAIL_3]"``.
    original:
        The raw PII value that was replaced, e.g. ``"alice@example.com"``.
    entity:
        The Presidio entity type label, e.g. ``"EMAIL_ADDRESS"``.

    Raises
    ------
    aiosqlite.OperationalError
        If the database file is locked or the schema hasn't been initialised.
    """
    now: str = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO pii_vault (token, original_value, entity_type, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, original, entity, now),
        )
        await conn.commit()

    logger.debug("Stored mapping  %s → [REDACTED] (%s)", token, entity)


async def get_original(token: str) -> str | None:
    """Look up the original PII value for a given token.

    Parameters
    ----------
    token:
        The placeholder token to resolve, e.g. ``"[PERSON_1]"``.

    Returns
    -------
    str | None
        The original PII string if found, or ``None`` if the token is unknown
        or the session has been cleared.

    Example
    -------
    .. code-block:: python

        original = await get_original("[PERSON_1]")
        if original is None:
            raise HTTPException(404, "Token not found — session may have expired.")
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT original_value FROM pii_vault WHERE token = ?",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        logger.warning("Token lookup miss: %s", token)
        return None

    logger.debug("Token resolved: %s → [REDACTED]", token)
    return str(row["original_value"])


async def clear_session() -> int:
    """Wipe **all** entries from the vault.

    This is a hard delete — use it for privacy testing or when a user
    explicitly ends their session.  Returns the number of rows deleted so
    callers can confirm the operation.

    .. warning::
        Irreversible.  All token→PII mappings are lost permanently.
        Guard this behind an admin route or a UI confirmation dialog.

    Returns
    -------
    int
        Number of rows deleted from ``pii_vault``.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute("DELETE FROM pii_vault")
        await conn.commit()
        deleted: int = cursor.rowcount

    logger.info("Vault cleared — %d mapping(s) removed.", deleted)
    return deleted
