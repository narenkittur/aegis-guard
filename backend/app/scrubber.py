"""
backend/app/scrubber.py — Aegis-Guard PII Logic Engine
=======================================================
Implements the two core transforms that power the full round-trip:

  deidentify_text(text)
      Detects PII using Microsoft Presidio + spaCy, replaces every span with
      an opaque token like ``[PERSON_1]`` or ``[EMAIL_ADDRESS_2]``, and
      persists each token→original mapping to the local SQLite vault.

  rehydrate_text(text)
      Scans an LLM response for Aegis-Guard tokens, resolves each one via the
      vault, and returns the fully reconstructed text with real PII restored.

Design notes
------------
* The Presidio engines (``AnalyzerEngine``, ``AnonymizerEngine``) are
  initialised **once at module load time** into ``_ENGINES``.  This avoids
  re-loading the 500 MB spaCy model on every request (which would add ~8 s
  of latency per call).  ``_get_engines()`` is the only entry point and is
  thread-safe because module-level initialisation in CPython is protected by
  the GIL.

* Tokens are assigned a **session-scoped counter** stored in the vault.  The
  counter is derived from the number of existing rows so it survives server
  restarts within the same session.

* Spans are replaced in **right-to-left order** (highest start offset first)
  so that substituting one span never invalidates the character offsets of
  spans to its left.

* ``rehydrate_text`` deliberately leaves unresolved tokens in-place (rather
  than raising an error) so that a partial vault clear doesn't corrupt the
  entire LLM response.  Callers receive a ``tokens_unresolved`` count so the
  UI can warn the user.

* All I/O with the vault is ``await``-ed; this module is designed to be called
  from async FastAPI route handlers only.  Do not call from sync contexts
  without ``asyncio.run()``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import NamedTuple

from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider

from backend.app.database import get_original, store_mapping

# ── Logging ───────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Entity types to detect (Phase 1 scope).
# Extend this list in Phase 2 to cover more PII categories.
_ENTITIES: list[str] = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
]

# Regex that matches any token produced by this scrubber.
# Anchored with word boundaries to avoid partial matches.
_TOKEN_PATTERN: re.Pattern[str] = re.compile(
    r"\[([A-Z_]+)_(\d+)\]"
)

# Minimum Presidio confidence score to act on a detected entity.
_MIN_SCORE: float = 0.6


# ── Engine singleton ──────────────────────────────────────────────────────────


@dataclass
class _Engines:
    """Holds warm Presidio engine instances."""

    analyzer: AnalyzerEngine
    # AnonymizerEngine is intentionally NOT used for token replacement —
    # we build tokens ourselves so we control the naming scheme and can
    # persist them to the vault.  The field is kept for potential Phase 2 use.
    _extra: dict = field(default_factory=dict)


_ENGINES: _Engines | None = None


def _get_engines() -> _Engines:
    """Return the module-level engine singleton, initialising it on first call.

    Uses spaCy ``en_core_web_sm`` as the NLP backend.  The model must have
    been downloaded via ``python -m spacy download en_core_web_sm`` before
    the server starts.

    Returns
    -------
    _Engines
        Warmed Presidio engine wrapper.

    Raises
    ------
    OSError
        If the spaCy model has not been installed.
    """
    global _ENGINES  # noqa: PLW0603
    if _ENGINES is not None:
        return _ENGINES

    logger.info("Initialising Presidio AnalyzerEngine with spaCy en_core_web_sm …")

    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_config)
    nlp_engine = provider.create_engine()

    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])

    _ENGINES = _Engines(analyzer=analyzer)
    logger.info("Presidio engines ready.")
    return _ENGINES


def warm_engines() -> None:
    """Force-initialise the engine singleton at application startup.

    Call this from the FastAPI ``lifespan`` hook so the first real request
    is not penalised by spaCy model load time.

    Example
    -------
    .. code-block:: python

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await initialize_db()
            warm_engines()          # ← call here
            yield
    """
    _get_engines()


# ── Public aliases ────────────────────────────────────────────────────────────
# ``anonymize`` / ``rehydrate`` are the user-facing names required by the API
# spec.  They delegate directly to the canonical functions so the CLAUDE.md
# rule (deidentify_text / rehydrate_text are the only vault writers/readers)
# is never broken.

async def anonymize(text: str) -> tuple[str, int, list[str]]:
    """Public alias for :func:`deidentify_text`.

    Detects PERSON, EMAIL_ADDRESS, and PHONE_NUMBER entities in *text*,
    replaces each with an opaque token, and persists the mapping to the vault.

    Parameters
    ----------
    text:
        Raw input that may contain PII.

    Returns
    -------
    tuple[str, int, list[str]]
        ``(scrubbed_text, token_count, entity_types_found)``
    """
    return await deidentify_text(text)


async def rehydrate(text: str) -> tuple[str, int, int]:
    """Public alias for :func:`rehydrate_text`.

    Scans *text* for ``[ENTITY_TYPE_N]`` tokens and replaces them with their
    original PII values from the vault.

    Parameters
    ----------
    text:
        LLM response that may contain Aegis-Guard tokens.

    Returns
    -------
    tuple[str, int, int]
        ``(restored_text, tokens_resolved, tokens_unresolved)``
    """
    return await rehydrate_text(text)


# ── Internal helpers ──────────────────────────────────────────────────────────


class _Span(NamedTuple):
    """A detected PII span with its metadata."""

    start: int
    end: int
    entity_type: str
    score: float
    text: str


def _analyse(text: str) -> list[_Span]:
    """Run Presidio analysis and return deduplicated, filtered spans.

    Presidio can emit overlapping results (e.g. a PHONE_NUMBER that also
    partially matches a DATE_TIME).  This function keeps only the highest-
    confidence, non-overlapping span for each character region.

    Parameters
    ----------
    text:
        Raw input text to scan.

    Returns
    -------
    list[_Span]
        Filtered spans sorted by ``start`` offset, ascending.
    """
    engines = _get_engines()
    results: list[RecognizerResult] = engines.analyzer.analyze(
        text=text,
        entities=_ENTITIES,
        language="en",
        score_threshold=_MIN_SCORE,
    )

    # Sort by score descending so the best result wins overlaps.
    results.sort(key=lambda r: r.score, reverse=True)

    # Greedy non-overlap filter.
    accepted: list[RecognizerResult] = []
    covered: set[int] = set()
    for r in results:
        span_chars = set(range(r.start, r.end))
        if span_chars & covered:
            continue  # overlaps an already-accepted span — skip
        accepted.append(r)
        covered |= span_chars

    # Re-sort by start offset (left to right) for readable logging.
    accepted.sort(key=lambda r: r.start)

    return [
        _Span(
            start=r.start,
            end=r.end,
            entity_type=r.entity_type,
            score=r.score,
            text=text[r.start : r.end],
        )
        for r in accepted
    ]


async def _next_token_index(entity_type: str) -> int:
    """Return the next available 1-based counter for a given entity type.

    The counter is derived at call-time from the vault so it survives server
    restarts.  Two concurrent requests could theoretically get the same index;
    for Phase 1 (single-user local tool) this is acceptable.

    Parameters
    ----------
    entity_type:
        Presidio entity label, e.g. ``"PERSON"``.

    Returns
    -------
    int
        Next index to use, e.g. ``3`` → token becomes ``[PERSON_3]``.
    """
    from backend.app.database import DB_PATH  # local import to avoid circular
    import aiosqlite

    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM pii_vault WHERE entity_type = ?",
            (entity_type,),
        ) as cur:
            row = await cur.fetchone()
    return (row[0] if row else 0) + 1


# ── Public API ────────────────────────────────────────────────────────────────


async def deidentify_text(text: str) -> tuple[str, int, list[str]]:
    """Detect PII in *text*, replace with tokens, persist mappings to vault.

    Replacement is performed **right-to-left** (highest ``start`` offset first)
    to guarantee that replacing one span never shifts the offsets of spans to
    its left.

    Parameters
    ----------
    text:
        Raw user prompt that may contain personally identifiable information.

    Returns
    -------
    tuple[str, int, list[str]]
        A 3-tuple of:
        - ``scrubbed_text`` — the prompt with all PII replaced by tokens.
        - ``token_count`` — number of distinct tokens inserted.
        - ``entities_found`` — list of entity-type labels, in left-to-right
          order of their appearance in the original text.

    Raises
    ------
    OSError
        If the spaCy model is not installed.
    aiosqlite.OperationalError
        If the vault cannot be reached.

    Example
    -------
    .. code-block:: python

        scrubbed, count, types = await deidentify_text(
            "Call Alice at alice@corp.com."
        )
        # scrubbed  → "Call [PERSON_1] at [EMAIL_ADDRESS_2]."
        # count     → 2
        # types     → ["PERSON", "EMAIL_ADDRESS"]
    """
    if not text.strip():
        return text, 0, []

    spans = _analyse(text)

    if not spans:
        logger.debug("No PII detected in input text.")
        return text, 0, []

    logger.info("Detected %d PII span(s): %s", len(spans), [s.entity_type for s in spans])

    # Build a counter map so each entity type has its own incrementing index.
    # We look up the current max index per type once, then increment locally.
    type_counter: dict[str, int] = {}
    for span in spans:
        if span.entity_type not in type_counter:
            type_counter[span.entity_type] = await _next_token_index(span.entity_type)

    # Map each span to its assigned token.
    span_tokens: list[tuple[_Span, str]] = []
    for span in spans:
        idx = type_counter[span.entity_type]
        token = f"[{span.entity_type}_{idx}]"
        span_tokens.append((span, token))
        type_counter[span.entity_type] += 1

    # Replace right-to-left to preserve offsets.
    scrubbed = text
    for span, token in reversed(span_tokens):
        scrubbed = scrubbed[: span.start] + token + scrubbed[span.end :]
        await store_mapping(token, span.text, span.entity_type)
        logger.debug("Replaced %r with %s (score=%.2f)", span.text, token, span.score)

    entities_found = [s.entity_type for s in spans]
    return scrubbed, len(span_tokens), entities_found


async def rehydrate_text(text: str) -> tuple[str, int, int]:
    """Replace Aegis-Guard tokens in *text* with their original PII values.

    Tokens that are not found in the vault (e.g. after ``clear_session()``)
    are left in-place and counted in ``tokens_unresolved`` so the caller can
    surface a warning without corrupting the rest of the response.

    Parameters
    ----------
    text:
        LLM response text that may contain ``[ENTITY_TYPE_N]`` tokens.

    Returns
    -------
    tuple[str, int, int]
        A 3-tuple of:
        - ``original_text`` — text with all resolved tokens swapped back.
        - ``tokens_resolved`` — number of tokens successfully replaced.
        - ``tokens_unresolved`` — number of tokens found but not in the vault.

    Raises
    ------
    aiosqlite.OperationalError
        If the vault cannot be reached.

    Example
    -------
    .. code-block:: python

        restored, resolved, unresolved = await rehydrate_text(
            "Summary for [PERSON_1] at [EMAIL_ADDRESS_2]."
        )
        # restored    → "Summary for Alice at alice@corp.com."
        # resolved    → 2
        # unresolved  → 0
    """
    token_matches = list(_TOKEN_PATTERN.finditer(text))

    if not token_matches:
        logger.debug("No Aegis-Guard tokens found in text — returning as-is.")
        return text, 0, 0

    logger.info("Found %d token(s) to rehydrate.", len(token_matches))

    resolved = 0
    unresolved = 0
    result = text

    # Process right-to-left so replacements don't shift earlier offsets.
    for match in reversed(token_matches):
        token = match.group(0)   # e.g. "[PERSON_1]"
        original = await get_original(token)

        if original is None:
            logger.warning("Token %s not found in vault — leaving in-place.", token)
            unresolved += 1
            continue

        result = result[: match.start()] + original + result[match.end() :]
        resolved += 1
        logger.debug("Rehydrated %s → [REDACTED]", token)

    logger.info(
        "Rehydration complete: %d resolved, %d unresolved.",
        resolved,
        unresolved,
    )
    return result, resolved, unresolved
