"""
backend/app/orchestrator.py — Aegis-Guard Cloud Integration
============================================================
Acts as the high-performance 'Shield' wrapping the Cloud LLM.

Pipeline (updated for Aegis-Vision):
  STEP 0  [Optional] — If the input is raw ``bytes`` (image/PDF), dispatch
                        to ``vision.py`` for in-memory OCR extraction and
                        capture ``ocr_latency_ms`` for the Privacy Tax metric.
  STEP 1  — Call the local /api/v1/scrub endpoint to de-identify text PII.
  STEP 2  — Log what stays local vs. what is sent to the cloud.
  STEP 3  — Forward the scrubbed text to the Gemini LLM.
  STEP 4  — Rehydrate the LLM response via the local /api/v1/restore endpoint.
  STEP 5  — Return a structured result ``Dict`` with the final text and
             latency metadata for the frontend Privacy Tax widget.

This guarantees the Cloud LLM never sees real PII, whether the input
originated as a typed prompt or an uploaded image/PDF.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Dict, Optional, Union

import google.genai as genai
import httpx
from dotenv import load_dotenv

from backend.app.config import get_settings
from backend.app.vision import OcrError, OcrResult, extract_text

# ── Logging Setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Configuration & Initialization ────────────────────────────────────────────

# Ensure .env variables are loaded (satisfying the dotenv requirement)
load_dotenv()
settings = get_settings()

# Initialize Gemini SDK
_genai_client: genai.Client | None = None
if settings.llm_api_key:
    _genai_client = genai.Client(api_key=settings.llm_api_key)
else:
    logger.warning("LLM_API_KEY is missing! Set it in your .env file.")

# We use gemini-3.1-flash-lite as it provides a generous free tier limit.
_GEMINI_MODEL = "gemini-2.0-flash"
_SCRUB_URL = "http://127.0.0.1:8000/api/v1/scrub"
_RESTORE_URL = "http://127.0.0.1:8000/api/v1/restore"


# ── Core Orchestrator Logic ───────────────────────────────────────────────────

async def process_secure_request(
    raw_user_input: Union[str, bytes],
    mime_hint: str = "",
) -> Dict[str, object]:
    """End-to-end pipeline: [OCR] -> Scrub -> Cloud LLM -> Rehydrate.

    Parameters
    ----------
    raw_user_input:
        Either a plain-text ``str`` prompt, or raw ``bytes`` of an uploaded
        image (PNG/JPEG/TIFF/BMP/WebP) or PDF document.  When bytes are
        supplied, Aegis-Vision runs OCR first and the extracted text is
        fed into the scrubber.
    mime_hint:
        Optional MIME type string (e.g. ``"application/pdf"``,
        ``"image/png"``).  Used by the OCR dispatcher to skip magic-byte
        detection when the Content-Type is already known.  Ignored when
        ``raw_user_input`` is a ``str``.

    Returns
    -------
    Dict[str, object]
        A structured result containing:
        - ``"final_text"``       — rehydrated LLM response (``str``).
        - ``"ocr_latency_ms"``   — OCR wall-clock time in ms (``float``),
                                   ``0.0`` when no file was processed.
        - ``"scrub_token_count"``— number of PII tokens inserted (``int``).
        - ``"ocr_page_count"``   — pages processed (``int``), 0 for text.
        - ``"ocr_warnings"``     — list of non-fatal OCR warnings (``list``).

    Raises
    ------
    OcrError
        If the uploaded file is unreadable, encrypted, or unsupported.
    RuntimeError
        If the local Scrubber/Vault is unreachable or Gemini fails.
    """
    logger.info("=== Starting Secure Orchestration Pipeline ===")

    # ── STEP 0: Aegis-Vision OCR Pre-Processing ───────────────────────────────
    # If the caller supplied raw bytes (an uploaded image or PDF), extract text
    # first.  The result feeds into the scrubber exactly like a typed prompt.
    ocr_result: Optional[OcrResult] = None
    text_for_scrubber: str

    if isinstance(raw_user_input, bytes):
        logger.info("STEP 0: File bytes detected — dispatching to Aegis-Vision OCR …")
        try:
            # run_in_executor keeps the blocking EasyOCR call off the event loop.
            loop = asyncio.get_running_loop()
            ocr_result = await loop.run_in_executor(
                None,
                lambda: extract_text(raw_user_input, mime_hint),
            )
        except OcrError as exc:
            logger.error("Aegis-Vision OCR failed: %s", exc)
            raise  # Re-raise OcrError directly; FastAPI maps it to HTTP 422
        except Exception as exc:
            logger.error("Unexpected error in Aegis-Vision: %s", exc)
            raise RuntimeError(f"OCR pipeline failure: {exc}") from exc

        text_for_scrubber = ocr_result.raw_text
        logger.info(
            "STEP 0 complete: %d page(s) extracted in %.0f ms — %d char(s) of raw text.",
            ocr_result.page_count,
            ocr_result.ocr_latency_ms,
            len(text_for_scrubber),
        )
    else:
        # Plain text prompt — skip OCR entirely.
        text_for_scrubber = raw_user_input
        logger.info("STEP 0: Plain-text input — OCR stage skipped.")

    # Use a single httpx AsyncClient for efficient connection pooling.
    async with httpx.AsyncClient(timeout=10.0) as client:

        # ── STEP 1: Call LOCAL scrub endpoint ────────────────────────────────
        logger.info("STEP 1: Calling local Scrubber...")
        try:
            scrub_res = await client.post(
                _SCRUB_URL,
                json={"text": text_for_scrubber},
            )
            scrub_res.raise_for_status()
            scrub_data = scrub_res.json()
        except httpx.RequestError as exc:
            logger.error("Failed to reach local Scrubber. Is FastAPI running on port 8000?")
            raise RuntimeError(f"Scrubber unreachable: {exc}") from exc

        scrubbed_text: str = scrub_data["scrubbed_text"]
        scrub_token_count: int = scrub_data.get("token_count", 0)

        # ── STEP 2: Log privacy boundary ─────────────────────────────────────
        logger.info("==========================================")
        logger.info("LOCAL INPUT   : %s", text_for_scrubber)
        logger.info("CLOUD OUTBOUND: %s", scrubbed_text)
        logger.info("==========================================")

        # ── STEP 3: Send the scrubbed text to Gemini ─────────────────────────
        logger.info("STEP 3: Sending scrubbed payload to %s...", _GEMINI_MODEL)
        if not _genai_client:
            raise RuntimeError("LLM_API_KEY not configured — cannot call Gemini.")
        try:
            loop = asyncio.get_running_loop()
            gemini_response = await loop.run_in_executor(
                None,
                lambda: _genai_client.models.generate_content(
                    model=_GEMINI_MODEL,
                    contents=scrubbed_text,
                ),
            )
            llm_text: str = gemini_response.text

            logger.info("==========================================")
            logger.info("CLOUD INBOUND : %s", llm_text)
            logger.info("==========================================")

        except Exception as exc:
            logger.error("Gemini API call failed. Check your API key and network connection.")
            raise RuntimeError(f"Cloud LLM failure: {exc}") from exc

        # ── STEP 4: Call LOCAL restore endpoint ──────────────────────────────
        logger.info("STEP 4: Rehydrating response via local Vault...")
        try:
            restore_res = await client.post(
                _RESTORE_URL,
                json={"text": llm_text},
            )
            restore_res.raise_for_status()
            restore_data = restore_res.json()
        except httpx.RequestError as exc:
            logger.error("Failed to reach local Vault for rehydration.")
            raise RuntimeError(f"Vault unreachable: {exc}") from exc

        final_text: str = restore_data["original_text"]

        # ── STEP 5: Build and return structured result ────────────────────────
        logger.info("==========================================")
        logger.info("FINAL OUTPUT  : %s", final_text)
        logger.info("==========================================")

        result: Dict[str, object] = {
            "final_text":        final_text,
            "extracted_text":    text_for_scrubber,
            "scrubbed_text":     scrubbed_text,
            "ocr_latency_ms":    ocr_result.ocr_latency_ms if ocr_result else 0.0,
            "scrub_token_count": scrub_token_count,
            "ocr_page_count":    ocr_result.page_count if ocr_result else 0,
            "ocr_warnings":      ocr_result.warnings if ocr_result else [],
        }
        logger.info(
            "Pipeline complete | ocr=%.0f ms | tokens=%d | pages=%d",
            result["ocr_latency_ms"],
            result["scrub_token_count"],
            result["ocr_page_count"],
        )
        return result


# ── Execution Block ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    # A realistic test case containing multiple PII types
    TEST_PROMPT = (
        "Write a polite email to John Doe thanking him for his recent purchase of Order #12345. "
        "Also mention that we will ship it to his home at 221B Baker Street "
        "and will reach out to him at 555-0199 if there are any issues with the delivery."
    )

    try:
        # Check if the FastAPI server is responsive before running the pipeline
        logger.info("Verifying local Aegis-Guard API is online...")
        # A quick synchronous check for the health endpoint
        r = httpx.get("http://127.0.0.1:8000/health", timeout=2.0)
        if r.status_code == 200:
            logger.info("Shield is ACTIVE. Commencing secure request...")
        else:
            logger.warning("Health check returned status %d", r.status_code)
    except httpx.RequestError:
        logger.error("Aegis-Guard API is down! Please run 'uvicorn backend.app.main:app --reload' first.")
        sys.exit(1)

    # Run the async orchestration
    payload = asyncio.run(process_secure_request(TEST_PROMPT))
    print("\n--- TEST COMPLETE ---")
    print(f"Final text    : {payload['final_text']}")
    print(f"OCR latency   : {payload['ocr_latency_ms']:.1f} ms")
    print(f"Scrub tokens  : {payload['scrub_token_count']}")
