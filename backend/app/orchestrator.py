"""
backend/app/orchestrator.py — Aegis-Guard Cloud Integration
============================================================
Acts as the high-performance 'Shield' wrapping the Cloud LLM.
It intercepts the user prompt, scrubs it via the local API, sends the
redacted text to Gemini, and then rehydrates the response before returning it.

This guarantees the Cloud LLM never sees the real PII.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import google.generativeai as genai
import httpx
from dotenv import load_dotenv

from backend.app.config import get_settings

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
if not settings.llm_api_key:
    logger.warning("LLM_API_KEY is missing! Set it in your .env file.")

genai.configure(api_key=settings.llm_api_key)

# We use gemini-1.5-pro for high performance/reasoning tasks.
_GEMINI_MODEL = "gemini-1.5-pro"
_SCRUB_URL = "http://127.0.0.1:8000/api/v1/scrub"
_RESTORE_URL = "http://127.0.0.1:8000/api/v1/restore"


# ── Core Orchestrator Logic ───────────────────────────────────────────────────

async def process_secure_request(raw_user_input: str) -> str:
    """End-to-end pipeline: Scrub -> Cloud LLM -> Rehydrate.

    Parameters
    ----------
    raw_user_input:
        The original text prompt containing real PII.

    Returns
    -------
    str
        The final, rehydrated LLM response with PII restored.
    """
    logger.info("=== Starting Secure Orchestration Pipeline ===")

    # Use a single httpx AsyncClient for efficient connection pooling.
    async with httpx.AsyncClient(timeout=10.0) as client:
        
        # STEP 1: Call LOCAL scrub endpoint
        logger.info("STEP 1: Calling local Scrubber...")
        try:
            scrub_res = await client.post(
                _SCRUB_URL,
                json={"text": raw_user_input}
            )
            scrub_res.raise_for_status()
            scrub_data = scrub_res.json()
        except httpx.RequestError as exc:
            logger.error("Failed to reach local Scrubber. Is FastAPI running on port 8000?")
            raise RuntimeError(f"Scrubber unreachable: {exc}") from exc
            
        scrubbed_text = scrub_data["scrubbed_text"]
        
        # STEP 2: Log what stays local vs. what goes to the cloud
        logger.info("==========================================")
        logger.info("LOCAL INPUT : %s", raw_user_input)
        logger.info("CLOUD OUTBOUND: %s", scrubbed_text)
        logger.info("==========================================")

        # STEP 3: Send the scrubbed text to Gemini
        logger.info("STEP 3: Sending scrubbed payload to %s...", _GEMINI_MODEL)
        try:
            # We run the synchronous Gemini call in an executor to avoid blocking the event loop
            model = genai.GenerativeModel(_GEMINI_MODEL)
            loop = asyncio.get_running_loop()
            gemini_response = await loop.run_in_executor(
                None, 
                lambda: model.generate_content(scrubbed_text)
            )
            llm_text = gemini_response.text
            
            logger.info("==========================================")
            logger.info("CLOUD INBOUND : %s", llm_text)
            logger.info("==========================================")
            
        except Exception as exc:
            logger.error("Gemini API call failed. Check your API key and network connection.")
            raise RuntimeError(f"Cloud LLM failure: {exc}") from exc

        # STEP 4: Call LOCAL restore endpoint to rehydrate the response
        logger.info("STEP 4: Rehydrating response via local Vault...")
        try:
            restore_res = await client.post(
                _RESTORE_URL,
                json={"text": llm_text}
            )
            restore_res.raise_for_status()
            restore_data = restore_res.json()
        except httpx.RequestError as exc:
            logger.error("Failed to reach local Vault for rehydration.")
            raise RuntimeError(f"Vault unreachable: {exc}") from exc

        final_text = restore_data["original_text"]
        
        # STEP 5: Return the final string
        logger.info("==========================================")
        logger.info("FINAL OUTPUT  : %s", final_text)
        logger.info("==========================================")
        
        return final_text


# ── Execution Block ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    # A realistic test case containing multiple PII types
    TEST_PROMPT = (
        "Write a polite email to John Doe thanking him for his recent purchase. "
        "Also mention that we have updated his contact email to john.doe@example.com "
        "and will reach out to him at 555-0199 if there are any issues."
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
    result = asyncio.run(process_secure_request(TEST_PROMPT))
    print("\n--- TEST COMPLETE ---")
