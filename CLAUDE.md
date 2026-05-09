# CLAUDE.md — Aegis-Guard Locked Architecture Rules
# =====================================================
# This file governs ALL future coding sessions on this project.
# No rule here may be changed without explicit user approval.
# Last approved: 2026-05-04

---

## Project Identity
- **Name:** Aegis-Guard
- **Purpose:** Local-first PII redaction gateway that sanitises sensitive data
  before it is sent to cloud LLMs, with full round-trip de-identification.
- **Language:** Python 3.11+
- **Package manager:** Poetry (`pyproject.toml`)

---

## Canonical Folder Structure

```
aegis-guard/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py          ← FastAPI app + all routes
│   │   ├── database.py      ← SQLite vault (aiosqlite, async CRUD)
│   │   ├── scrubber.py      ← PII detection + token round-trip logic
│   │   ├── encryption.py    ← AES-256-GCM helpers (Phase 2)
│   │   └── config.py        ← pydantic-settings / .env loader
│   └── tests/
│       ├── __init__.py
│       ├── test_scrubber.py
│       └── test_database.py
├── frontend/
│   ├── Home.py              ← Main Streamlit chat UI
│   └── pages/
│       └── 1_Vault_Inspector.py
├── shared/
│   └── models.py            ← Pydantic schemas (shared by both layers)
├── .env.example
├── pyproject.toml
├── CLAUDE.md                ← THIS FILE
└── README.md
```

---

## Inviolable Architecture Rules

### 1. Separation of Concerns
- `frontend/` and `backend/` are ALWAYS separate processes.
- The Streamlit UI MUST NEVER import from `backend/` directly.
- All frontend→backend communication goes through HTTP (`httpx` → FastAPI).

### 2. Scrubber Engine
- Presidio `AnalyzerEngine` and `AnonymizerEngine` are initialised **once**
  at module level (or at app startup via `lifespan`), never per-request.
- Token format is strictly `[ENTITY_TYPE_N]` — e.g., `[PERSON_1]`, `[EMAIL_ADDRESS_2]`.
- Spans are replaced **right-to-left** to prevent index drift.
- `deidentify_text()` is the ONLY function allowed to write to the vault.
- `rehydrate_text()` is the ONLY function allowed to read tokens from the vault.

### 3. Vault (database.py)
- ALWAYS use `aiosqlite` — no synchronous `sqlite3` calls.
- Every function opens and closes its own `async with aiosqlite.connect()`.
- No connection is held open between requests.
- `INSERT OR IGNORE` for `store_mapping()` — idempotent by design.
- Phase 1: plain-text `original_value`. Phase 2: AES-256-GCM ciphertext.

### 4. Configuration
- ALL secrets (API keys, encryption key) come from `.env` via `config.py`.
- `get_settings()` is cached with `@lru_cache`.
- NEVER hard-code secrets or model names anywhere else.

### 5. Approved Technology Choices

| Concern             | Library                          | Notes                          |
|---------------------|----------------------------------|--------------------------------|
| Web framework       | FastAPI ^0.111                   | Locked                         |
| ASGI server         | uvicorn[standard] ^0.30          | Locked                         |
| Async DB            | aiosqlite ^0.20                  | Locked                         |
| PII detection       | presidio-analyzer ^2.2.355       | Locked                         |
| PII anonymisation   | presidio-anonymizer ^2.2.355     | Locked                         |
| NLP model           | spaCy en_core_web_lg             | lg tier, NOT sm                |
| Encryption (Ph. 2)  | cryptography ^42 (AES-256-GCM)   | Locked                         |
| KDF (Ph. 2)         | argon2-cffi ^23                  | Locked                         |
| Frontend            | streamlit ^1.35                  | Locked                         |
| HTTP client         | httpx ^0.27                      | Locked                         |
| Settings            | pydantic-settings ^2.3           | Locked                         |
| Linter              | ruff (line-length = 100)         | Locked                         |
| Type checker        | mypy strict                      | Locked                         |
| Test framework      | pytest-asyncio (asyncio_mode=auto)| Locked                        |

### 6. Code Style
- All new files start with a module-level docstring explaining purpose.
- All public functions have NumPy-style docstrings with Parameters/Returns/Raises.
- Type annotations on every function signature (mypy strict must pass).
- No bare `except:` — always catch specific exception types.
- Log with `logging.getLogger(__name__)`, never `print()`.

### 7. Phase Gates
- **Phase 1 (current):** Vault plain-text, no auth, localhost only.
- **Phase 2:** AES-256-GCM vault encryption, passphrase KDF, admin token auth.
- No Phase 2 code is written until Phase 1 is fully tested.

---

## Open Decisions (Approved 2026-05-04)

| # | Decision              | Approved Choice         |
|---|-----------------------|-------------------------|
| 1 | spaCy model tier      | `en_core_web_lg`        |
| 2 | Vault encryption      | Plain-text (Phase 1)    |
| 3 | Token counter scope   | Per-session             |
| 4 | LLM target            | Pluggable via config    |
| 5 | Admin route auth      | None (localhost, Ph. 1) |
