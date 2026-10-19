# Aegis-Guard 🛡️
### A Privacy Firewall for Large Language Models

Aegis-Guard is a privacy-preserving middleware that detects and redacts sensitive information from user prompts **before they are sent to cloud-based LLMs**. It uses reversible anonymization to restore original information in generated responses, helping users interact with AI while reducing unnecessary exposure of personal and confidential data.

## ✨ Key Features

- **Hybrid PII Detection:** Combines Regex, Microsoft Presidio, spaCy NER, and GLiNER to identify sensitive information.
- **Reversible Redaction:** Replaces detected entities with placeholders and restores original values in LLM responses.
- **Local Data Vault:** Uses SQLite to store placeholder-to-original mappings.
- **LLM Integration:** Connects to Gemini 3.1 Flash-Lite through FastAPI middleware.
- **Document Processing:** Supports text extraction from images and PDFs using EasyOCR.
- **Privacy Dashboard:** Provides a Streamlit interface for interacting with the system and monitoring entity counts, redaction density, and latency.

## ⚙️ How It Works

1. **Input:** The user submits a prompt or supported document.
2. **Detection:** The hybrid pipeline identifies potentially sensitive entities.
3. **Redaction:** Detected values are replaced with unique placeholders and mapped in the local SQLite vault.
4. **LLM Processing:** The sanitized prompt is forwarded to the configured LLM.
5. **Restoration:** Original values are restored in the generated response using the stored mappings.

## 🧰 Tech Stack

**Backend:** Python, FastAPI, Uvicorn  
**NLP & Detection:** spaCy, GLiNER, Microsoft Presidio, Regex  
**Database:** SQLite  
**LLM Integration:** Gemini 3.1 Flash-Lite  
**Frontend:** Streamlit  
**OCR:** EasyOCR

## 📊 Evaluation

The project report records the following PII detection results:

- **Recall:** 98.2%
- **Precision:** 96.5%

*These figures should be interpreted in the context of the project's evaluation dataset and methodology.*

## 🚀 Getting Started

1. Clone the repository:
   ```bash
   git clone <YOUR_REPOSITORY_URL>
   cd Aegis-Guard
   ```
2. Create and activate a virtual environment.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Configure the required API credentials using environment variables.
5. Start the FastAPI backend and Streamlit interface using the commands specified in your project configuration.

## 🔒 Privacy Design

Sensitive values detected by the pipeline are replaced before the prompt is forwarded to the LLM. Original values are retained in a local SQLite mapping vault for response restoration.

**Note:** Detection is not guaranteed to catch every sensitive value. Aegis-Guard is a prototype privacy layer, not a substitute for comprehensive security controls.

## 👨‍💻 Project

Developed as part of an experiential learning project at RV College of Engineering.

