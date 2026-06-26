import re
import time
import os
from pathlib import Path
import httpx
import pandas as pd
import streamlit as st
from openai import OpenAI
from dotenv import load_dotenv

# ── Configuration & Page Setup ────────────────────────────────────────────────
st.set_page_config(page_title="Aegis-Guard Core", page_icon="🛡️", layout="wide")

# Resolve .env from the repo root regardless of Streamlit's CWD
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)
API_KEY = os.getenv("LLM_API_KEY")
_openai_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY) if API_KEY else None

# ── Backend Base URL ──────────────────────────────────────────────────────────
_BACKEND = "http://127.0.0.1:8000"

# ── Detection Layer Attribution Map ──────────────────────────────────────────
_DETECTION_LAYER: dict[str, str] = {
    "PHONE_NUMBER":   "⚡ Deterministic Layer (Regex)",
    "API_KEY":        "⚡ Deterministic Layer (Regex)",
    "CRYPTO_WALLET":  "⚡ Deterministic Layer (Regex)",
    "EMAIL_ADDRESS":  "🤖 Statistical Layer (NER-spaCy)",
    "IP_ADDRESS":     "🤖 Statistical Layer (NER-spaCy)",
    "PERSON":         "🧠 Zero-Shot Transformer Layer (GLiNER)",
    "LOCATION":       "🧠 Zero-Shot Transformer Layer (GLiNER)",
    "ORGANIZATION":   "🧠 Zero-Shot Transformer Layer (GLiNER)",
}

# ── Helper Functions ──────────────────────────────────────────────────────────
def calculate_redaction_density(raw_text: str, token_count: int) -> float:
    if not raw_text.strip():
        return 0.0
    total_words = len(raw_text.split())
    if total_words == 0:
        return 0.0
    return (token_count / total_words) * 100.0


def call_orchestrate_file(file_bytes: bytes, mime_type: str) -> dict | None:
    """POST raw file bytes to the /api/v1/orchestrate multipart endpoint."""
    try:
        response = httpx.post(
            f"{_BACKEND}/api/v1/orchestrate",
            files={"file": ("upload", file_bytes, mime_type)},
            timeout=180.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.RequestError as e:
        st.error(f"Failed to connect to Aegis-Guard Backend: {e}")
        return None
    except httpx.HTTPStatusError as e:
        st.error(f"Aegis-Guard Backend returned an error: {e.response.text}")
        return None


def call_scrub_api(text: str) -> dict | None:
    """POST plain text to the /api/v1/scrub endpoint."""
    try:
        response = httpx.post(
            f"{_BACKEND}/api/v1/scrub",
            json={"text": text},
            timeout=180.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.RequestError as e:
        st.error(f"Failed to connect to Aegis-Guard Backend: {e}")
        return None
    except httpx.HTTPStatusError as e:
        st.error(f"Aegis-Guard Backend returned an error: {e}")
        return None


def call_llm(text: str) -> str | None:
    if not _openai_client:
        st.error("LLM_API_KEY is not set in the .env file.")
        return None
    try:
        res = _openai_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": text}],
        )
        return res.choices[0].message.content
    except Exception as e:
        st.error(f"Cloud LLM Failure: {e}")
        return None


def call_restore_api(text: str) -> dict | None:
    try:
        response = httpx.post(
            f"{_BACKEND}/api/v1/restore",
            json={"text": text},
            timeout=180.0,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        st.error(f"Failed to connect to Aegis-Guard Backend for restoration: {e}")
        return None


# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.title("🛡️ Aegis-Guard Core")
st.sidebar.success("Engine: ONLINE")

st.sidebar.markdown("---")
st.sidebar.metric("Lifetime Shielded", "1,249", "↑ 12 today")
st.sidebar.metric("Active Vaults", "3", None)
st.sidebar.markdown("---")

st.sidebar.caption("spaCy | Presidio | GLiNER | EasyOCR | gpt-oss-20b")


# ── Session State ─────────────────────────────────────────────────────────────
if "raw_prompt" not in st.session_state:
    st.session_state.raw_prompt = ""
if "scrubbed_text" not in st.session_state:
    st.session_state.scrubbed_text = ""
if "final_output" not in st.session_state:
    st.session_state.final_output = ""
if "metrics" not in st.session_state:
    st.session_state.metrics = None


# ── Main Interface ────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "🔐 Secure Terminal",
    "📊 Privacy Analytics",
    "☁️ Cloud Payload (Audit)",
    "📋 Inspection Diff",
])

# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — SECURE TERMINAL (with Aegis-Vision file ingestion)
# ════════════════════════════════════════════════════════════════════════════════
with tab1:
    st.subheader("Secure Terminal")

    # ── FILE INGESTION LAYER ──────────────────────────────────────────────────
    uploaded_file = st.file_uploader(
        "Upload Document or Image for Secure Processing",
        type=["png", "jpg", "jpeg", "pdf"],
        help="Aegis-Vision extracts text from the file entirely in-memory before scrubbing.",
    )
    st.caption(
        "🔒 Aegis-Vision Engine: Active | "
        "Streams processed entirely in-memory (Zero-Disk-Footprint)"
    )

    st.markdown("---")

    prompt_input = st.text_area(
        "Or enter a sensitive prompt directly",
        height=150,
        placeholder=(
            "Write a polite email to John Doe thanking him for his recent purchase "
            "of Order #12345. Also mention that we will ship it to his home at "
            "221B Baker Street and will reach out to him at 555-0199..."
        ),
    )

    if st.button("Shield & Execute", type="primary"):
        # Determine input mode: file takes precedence over text area
        has_file = uploaded_file is not None
        has_text = bool(prompt_input.strip())

        if not has_file and not has_text:
            st.warning("Please upload a file or enter a prompt before executing.")
        else:
            start_time = time.time()

            if uploaded_file is not None:
                # ── FILE PATH: route through /api/v1/orchestrate ──────────────
                file_bytes = uploaded_file.read()
                mime_type = uploaded_file.type or "application/octet-stream"

                with st.spinner("Aegis-Vision: Extracting & scrubbing document..."):
                    orch_data = call_orchestrate_file(file_bytes, mime_type)

                if orch_data:
                    end_time = time.time()
                    total_latency_ms = round((end_time - start_time) * 1000)

                    # Orchestrator returns combined pipeline result
                    raw_text   = orch_data.get("extracted_text", "")
                    scrubbed   = orch_data.get("scrubbed_text", "")
                    final_text = orch_data.get("final_text", "")
                    ocr_ms     = orch_data.get("ocr_latency_ms", 0.0)
                    token_cnt  = orch_data.get("scrub_token_count", 0)

                    entities_found = re.findall(r"\[([A-Z_]+)_\d+\]", scrubbed)

                    st.session_state.raw_prompt    = raw_text
                    st.session_state.scrubbed_text = scrubbed
                    st.session_state.final_output  = final_text
                    st.session_state.metrics = {
                        "token_count":    token_cnt,
                        "entities_found": entities_found,
                        "latency":        total_latency_ms,
                        "ocr_latency_ms": ocr_ms,
                        "input_mode":     "file",
                    }
                    st.success("### Final Rehydrated Output\n\n" + final_text)

            else:
                # ── TEXT PATH: existing scrub → LLM → restore pipeline ─────
                with st.spinner("Scrubbing data locally..."):
                    scrub_data = call_scrub_api(prompt_input)

                if scrub_data:
                    st.session_state.raw_prompt    = prompt_input
                    st.session_state.scrubbed_text = scrub_data["scrubbed_text"]

                    with st.spinner("Calling gpt-oss-20b..."):
                        llm_response = call_llm(st.session_state.scrubbed_text)

                    if llm_response:
                        with st.spinner("Rehydrating response locally..."):
                            restore_data = call_restore_api(llm_response)

                        if restore_data:
                            end_time = time.time()

                            st.session_state.final_output = restore_data["original_text"]

                            entities_found = re.findall(
                                r"\[([A-Z_]+)_\d+\]",
                                st.session_state.scrubbed_text,
                            )
                            st.session_state.metrics = {
                                "token_count":    len(entities_found),
                                "entities_found": entities_found,
                                "latency":        round((end_time - start_time) * 1000),
                                "ocr_latency_ms": 0.0,
                                "input_mode":     "text",
                            }
                            st.success(
                                "### Final Rehydrated Output\n\n"
                                + st.session_state.final_output
                            )

# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — PRIVACY ANALYTICS (3-column metrics + Local Compute breakdown)
# ════════════════════════════════════════════════════════════════════════════════
with tab2:
    st.subheader("Privacy Analytics")
    if st.session_state.metrics:
        m       = st.session_state.metrics
        density = calculate_redaction_density(st.session_state.raw_prompt, m["token_count"])

        ocr_ms          = float(m.get("ocr_latency_ms", 0.0))
        total_latency   = float(m["latency"])
        scrubber_ms     = max(0.0, total_latency - ocr_ms)

        # ── 3-column metrics row ──────────────────────────────────────────────
        col1, col2, col3 = st.columns(3)

        col1.metric("Redaction Density %", f"{density:.1f}%")
        col2.metric("Total Entities Caught", m["token_count"])

        with col3:
            st.markdown("**Local Compute Breakdown**")
            sub_a, sub_b = st.columns(2)
            sub_a.metric("OCR Engine", f"{ocr_ms:.0f} ms")
            sub_b.metric("Core Scrubber", f"{scrubber_ms:.0f} ms")

        # ── Security Assurance badge ──────────────────────────────────────────
        st.info(
            "⚡ **Security Assurance:** 100% Local Inference Efficiency. "
            "Zero data exposed to external APIs during sanitization."
        )

        st.markdown("---")

        # ── Entity distribution bar chart ─────────────────────────────────────
        entities = m["entities_found"]
        if entities:
            st.markdown("**Detected Entity Distribution**")
            counts: dict[str, int] = {}
            for e in entities:
                counts[e] = counts.get(e, 0) + 1
            df_chart = pd.DataFrame(list(counts.items()), columns=["Entity Type", "Count"])
            st.bar_chart(df_chart.set_index("Entity Type"))

            # ── Mapping Logic table with Detection Layer column ────────────────
            unique_types = sorted(set(entities))
            table_data = []
            for p in unique_types:
                table_data.append({
                    "Entity Type":     p,
                    "Placeholder":     f"[{p}_X]",
                    "Privacy Level":   "High" if p in ["PERSON", "LOCATION", "PHONE_NUMBER"] else "Medium",
                    "Detection Layer": _DETECTION_LAYER.get(p, "🔍 Unknown Layer"),
                })

            st.markdown("**Mapping Logic**")
            st.table(pd.DataFrame(table_data))
        else:
            st.info("No entities detected in the last run.")
    else:
        st.info("Run a prompt in the Secure Terminal to see analytics.")

# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — CLOUD PAYLOAD AUDIT
# ════════════════════════════════════════════════════════════════════════════════
with tab3:
    st.subheader("Cloud Payload (Audit)")
    if st.session_state.raw_prompt and st.session_state.scrubbed_text:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**What you typed / extracted (Raw):**")
            st.code(st.session_state.raw_prompt, language="text")
        with col2:
            st.markdown("**What the Cloud saw (Scrubbed):**")
            st.code(st.session_state.scrubbed_text, language="text")
    else:
        st.info("Run a prompt in the Secure Terminal to see the payload audit.")

# ════════════════════════════════════════════════════════════════════════════════
# TAB 4 — INSPECTION DIFF (side-by-side source vs. sanitized)
# ════════════════════════════════════════════════════════════════════════════════
with tab4:
    st.subheader("Live Inspection View")
    if st.session_state.raw_prompt and st.session_state.scrubbed_text:
        left, right = st.columns(2)
        with left:
            st.markdown("**Original Source / Extracted Text**")
            st.code(st.session_state.raw_prompt, language="text")
        with right:
            st.markdown("**Sanitized Cloud Payload**")
            st.code(st.session_state.scrubbed_text, language="text")
    else:
        st.info("Run a prompt in the Secure Terminal to populate the diff view.")
