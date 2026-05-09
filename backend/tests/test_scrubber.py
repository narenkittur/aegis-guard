"""
backend/tests/test_scrubber.py — Scrubber Unit Tests
=====================================================
Tests for deidentify_text() and rehydrate_text().
Uses a temporary in-memory vault (monkeypatched DB_PATH) so tests
never touch the real vault.db.
"""

from __future__ import annotations

import re
import pytest
import backend.app.database as db_module
from backend.app.scrubber import deidentify_text, rehydrate_text


@pytest.fixture(autouse=True)
def tmp_vault(tmp_path, monkeypatch):
    """Point the vault at a fresh temp file for every test."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test_vault.db")
    # Also patch the import inside scrubber.py
    import backend.app.scrubber as scrubber_module
    monkeypatch.setattr(scrubber_module, "store_mapping", db_module.store_mapping)
    monkeypatch.setattr(scrubber_module, "get_original", db_module.get_original)


@pytest.fixture(autouse=True)
async def init_vault():
    """Initialise the schema before each test."""
    await db_module.initialize_db()


# ── Token format ──────────────────────────────────────────────────────────────

TOKEN_PATTERN = re.compile(r"\[[A-Z_]+_\d+\]")


class TestDeidentify:
    async def test_returns_three_tuple(self):
        result = await deidentify_text("Hello world.")
        assert isinstance(result, tuple) and len(result) == 3

    async def test_no_pii_passthrough(self):
        text = "The sky is blue and the grass is green."
        scrubbed, count, entities = await deidentify_text(text)
        assert scrubbed == text
        assert count == 0
        assert entities == []

    async def test_person_detected(self):
        scrubbed, count, entities = await deidentify_text(
            "My name is Jonathan Harker."
        )
        assert count >= 1
        assert "PERSON" in entities
        assert "Jonathan Harker" not in scrubbed
        assert TOKEN_PATTERN.search(scrubbed)

    async def test_email_detected(self):
        scrubbed, count, entities = await deidentify_text(
            "Reach me at test.user@example.com anytime."
        )
        assert "EMAIL_ADDRESS" in entities
        assert "test.user@example.com" not in scrubbed

    async def test_multiple_entities(self):
        text = "Alice (alice@corp.com) and Bob discussed project X."
        scrubbed, count, entities = await deidentify_text(text)
        assert count >= 2

    async def test_token_format(self):
        scrubbed, count, _ = await deidentify_text("Contact John Smith please.")
        tokens = TOKEN_PATTERN.findall(scrubbed)
        assert all(TOKEN_PATTERN.fullmatch(t) for t in tokens)

    async def test_empty_string(self):
        scrubbed, count, entities = await deidentify_text("")
        assert scrubbed == ""
        assert count == 0

    async def test_whitespace_only(self):
        scrubbed, count, _ = await deidentify_text("   ")
        assert count == 0


class TestRehydrate:
    async def test_full_round_trip(self):
        original = "Call Alice at alice@corp.com for the meeting."
        scrubbed, _, _ = await deidentify_text(original)
        restored, resolved, unresolved = await rehydrate_text(scrubbed)

        assert "Alice" in restored
        assert "alice@corp.com" in restored
        assert resolved >= 1
        assert unresolved == 0

    async def test_no_tokens_passthrough(self):
        text = "No tokens here at all."
        restored, resolved, unresolved = await rehydrate_text(text)
        assert restored == text
        assert resolved == 0
        assert unresolved == 0

    async def test_unknown_token_left_in_place(self):
        text = "Hello [PERSON_999] how are you?"
        restored, resolved, unresolved = await rehydrate_text(text)
        assert "[PERSON_999]" in restored
        assert unresolved == 1
        assert resolved == 0

    async def test_partial_vault_clear(self):
        """After clear_session, previously-scrubbed tokens become unresolvable."""
        scrubbed, count, _ = await deidentify_text("Bob Smith called.")
        await db_module.clear_session()
        restored, resolved, unresolved = await rehydrate_text(scrubbed)
        assert unresolved == count
        assert resolved == 0
