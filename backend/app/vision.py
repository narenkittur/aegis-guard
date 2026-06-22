"""
backend/app/vision.py — Aegis-Vision OCR Engine
================================================
Provides in-memory OCR extraction from image bytes or PDF byte-streams,
feeding raw text into the downstream scrubber pipeline without ever
writing temporary files to disk.

Public API
----------
extract_text(file_bytes, mime_hint) -> OcrResult
    Dispatch function: detects PDF vs. image, runs the appropriate
    extraction path, and returns a structured ``OcrResult`` with the
    extracted text and latency metadata.

warm_reader() -> None
    Force-initialise the EasyOCR singleton at server startup so the
    first real request is not penalised by model-weight load time
    (~3–5 s on first call).

Design notes
------------
* ``_ReaderSingleton`` wraps ``easyocr.Reader(['en'])`` and initialises
  it **exactly once** at the class level via a ``_instance`` class
  variable and a lock-guarded ``__new__``.  The GIL alone is not
  sufficient here because ``easyocr.Reader.__init__`` is not
  idempotent; double-initialisation wastes GPU/CPU memory and time.
  A ``threading.Lock`` guards the critical section.

* PDF pages are converted with ``pdf2image.convert_from_bytes``, which
  internally calls Poppler and returns a list of ``PIL.Image`` objects
  held entirely in RAM.  Each image is then handed directly to
  ``reader.readtext(..., detail=0)`` as a NumPy array — no temp file
  paths involved.

* ``OcrResult.ocr_latency_ms`` is measured with ``time.perf_counter``
  (sub-millisecond precision) and is intentionally separate from the
  scrubber latency so the frontend can display both phases of the
  "Privacy Tax" metric independently.

* All exceptions from corrupted, encrypted, or zero-page files are
  caught and re-raised as ``OcrError`` (a ``ValueError`` subclass) so
  the orchestrator can surface a clean HTTP 422 without crashing the
  server thread.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Final

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# ── Public sentinel for known MIME types ──────────────────────────────────────

_PDF_MIME: Final[str] = "application/pdf"
_IMAGE_MIMES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/tiff", "image/bmp", "image/webp"}
)


# ── Structured error type ─────────────────────────────────────────────────────


class OcrError(ValueError):
    """Raised for unreadable, encrypted, or zero-content file streams.

    Inherits from ``ValueError`` so FastAPI's default exception handler
    surfaces it as HTTP 422 Unprocessable Entity without extra middleware.
    """


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class OcrResult:
    """Structured return value from :func:`extract_text`.

    Attributes
    ----------
    raw_text:
        Concatenated OCR output, newline-separated per page.
    page_count:
        Number of pages (always 1 for a single image input).
    ocr_latency_ms:
        Wall-clock time spent inside the EasyOCR model, in milliseconds.
        Feed this into the frontend Privacy Tax metric alongside the
        scrubber's tokenization latency.
    mime_detected:
        The MIME string that was actually used to dispatch the extraction.
    warnings:
        Non-fatal issues encountered during extraction (e.g. blank pages).
    """

    raw_text: str
    page_count: int
    ocr_latency_ms: float
    mime_detected: str
    warnings: list[str] = field(default_factory=list)


# ── EasyOCR Singleton ─────────────────────────────────────────────────────────


class _ReaderSingleton:
    """Thread-safe singleton wrapping ``easyocr.Reader``.

    The heavy model weights (~120 MB for English) are downloaded and
    cached by EasyOCR on first instantiation.  Subsequent calls to
    ``get()`` return the already-warm instance in O(1).

    Usage
    -----
    .. code-block:: python

        reader = _ReaderSingleton.get()
        lines = reader.readtext(np_image, detail=0)
    """

    _instance: "_ReaderSingleton | None" = None
    _lock: threading.Lock = threading.Lock()
    _reader: "easyocr.Reader | None" = None  # type: ignore[name-defined]  # noqa: F821

    def __new__(cls) -> "_ReaderSingleton":
        # Double-checked locking pattern — avoids acquiring the lock on
        # every subsequent call after the singleton is initialised.
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    cls._instance = obj
        return cls._instance  # type: ignore[return-value]

    def _ensure_reader(self) -> None:
        """Initialise the underlying EasyOCR reader if not yet done."""
        if self._reader is not None:
            return
        with self._lock:
            if self._reader is not None:
                return
            try:
                import easyocr  # local import: keeps startup fast if OCR unused

                logger.info(
                    "Aegis-Vision: Initialising EasyOCR Reader for ['en'] — "
                    "this will download model weights on first run …"
                )
                t0 = time.perf_counter()
                self.__class__._reader = easyocr.Reader(
                    ["en"],
                    gpu=False,  # CPU-safe default; set gpu=True if CUDA available
                    verbose=False,
                )
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("Aegis-Vision: EasyOCR ready in %.0f ms.", elapsed)
            except ImportError as exc:
                raise OcrError(
                    "easyocr is not installed. "
                    "Run: pip install easyocr"
                ) from exc

    @property
    def reader(self) -> "easyocr.Reader":  # type: ignore[name-defined]  # noqa: F821
        """Return the warm EasyOCR reader instance."""
        self._ensure_reader()
        assert self._reader is not None  # guaranteed by _ensure_reader
        return self._reader

    @classmethod
    def get(cls) -> "_ReaderSingleton":
        """Return the singleton, creating it on first call."""
        return cls()


# ── Internal helpers ──────────────────────────────────────────────────────────


def _pil_to_numpy(image: Image.Image) -> np.ndarray:
    """Convert a PIL Image to an RGB NumPy array for EasyOCR.

    Parameters
    ----------
    image:
        Source PIL image (any mode).

    Returns
    -------
    np.ndarray
        uint8 array of shape (H, W, 3).
    """
    return np.array(image.convert("RGB"))


def _ocr_image(np_image: np.ndarray) -> str:
    """Run EasyOCR on a single NumPy image array.

    Parameters
    ----------
    np_image:
        uint8 RGB array produced by :func:`_pil_to_numpy`.

    Returns
    -------
    str
        Detected text lines joined by newlines, or empty string if none.
    """
    reader_singleton = _ReaderSingleton.get()
    lines: list[str] = reader_singleton.reader.readtext(np_image, detail=0)
    return "\n".join(line.strip() for line in lines if line.strip())


def _extract_from_image_bytes(file_bytes: bytes) -> tuple[str, list[str]]:
    """Decode image bytes and run OCR.

    Parameters
    ----------
    file_bytes:
        Raw bytes of a PNG/JPEG/TIFF/BMP/WebP file.

    Returns
    -------
    tuple[str, list[str]]
        ``(extracted_text, warnings)``

    Raises
    ------
    OcrError
        If the bytes cannot be decoded as a valid image.
    """
    warnings: list[str] = []
    try:
        image = Image.open(io.BytesIO(file_bytes))
    except Exception as exc:
        raise OcrError(
            f"Cannot decode image stream: {exc}. "
            "File may be corrupted or in an unsupported format."
        ) from exc

    np_image = _pil_to_numpy(image)
    text = _ocr_image(np_image)

    if not text.strip():
        warnings.append("No text detected in the image — it may be blank or purely graphical.")

    return text, warnings


def _extract_from_pdf_bytes(file_bytes: bytes) -> tuple[str, int, list[str]]:
    """Convert PDF pages to in-memory PIL images and OCR each one.

    Uses ``pdf2image.convert_from_bytes`` so Poppler renders each page
    into a ``PIL.Image`` without any disk I/O.

    Parameters
    ----------
    file_bytes:
        Raw bytes of a PDF document.

    Returns
    -------
    tuple[str, int, list[str]]
        ``(extracted_text, page_count, warnings)``

    Raises
    ------
    OcrError
        If the PDF is encrypted, corrupt, or Poppler is not installed.
    """
    try:
        from pdf2image import convert_from_bytes  # local import
        from pdf2image.exceptions import (
            PDFInfoNotInstalledError,
            PDFPageCountError,
            PDFSyntaxError,
        )
    except ImportError as exc:
        raise OcrError(
            "pdf2image is not installed. "
            "Run: pip install pdf2image  (and install Poppler on your system)"
        ) from exc

    warnings: list[str] = []

    try:
        pages: list[Image.Image] = convert_from_bytes(
            file_bytes,
            dpi=200,           # 200 DPI: good OCR quality without excessive RAM
            fmt="jpeg",        # JPEG in memory — lower RAM than raw BMP
            thread_count=1,    # Single-threaded: predictable in async context
        )
    except PDFInfoNotInstalledError as exc:
        raise OcrError(
            "Poppler (pdfinfo) is not installed or not on PATH. "
            "Install it via: choco install poppler  (Windows) or apt-get install poppler-utils"
        ) from exc
    except PDFSyntaxError as exc:
        raise OcrError(f"PDF is malformed or corrupt: {exc}") from exc
    except PDFPageCountError as exc:
        raise OcrError(
            f"Cannot read PDF — it may be encrypted or password-protected: {exc}"
        ) from exc
    except Exception as exc:
        raise OcrError(f"Unexpected error processing PDF: {exc}") from exc

    if not pages:
        raise OcrError("PDF produced zero renderable pages — it may be blank or encrypted.")

    page_texts: list[str] = []
    for page_num, pil_page in enumerate(pages, start=1):
        np_page = _pil_to_numpy(pil_page)
        page_text = _ocr_image(np_page)
        if not page_text.strip():
            warnings.append(f"Page {page_num}: no text detected (possibly graphical or blank).")
        else:
            page_texts.append(page_text)

    full_text = "\n\n".join(page_texts)
    return full_text, len(pages), warnings


# ── MIME detection helper ─────────────────────────────────────────────────────


def _sniff_mime(file_bytes: bytes, mime_hint: str) -> str:
    """Resolve the effective MIME type from hint + magic bytes.

    If ``mime_hint`` is already a known PDF or image type, it is used
    as-is.  Otherwise the function falls back to sniffing the magic
    bytes (PDF: ``%PDF``; PNG: ``\\x89PNG``; JPEG: ``\\xff\\xd8``).

    Parameters
    ----------
    file_bytes:
        The raw file content — only the first 8 bytes are examined.
    mime_hint:
        MIME type string provided by the caller (may be empty/unknown).

    Returns
    -------
    str
        Resolved MIME string, e.g. ``"application/pdf"`` or ``"image/jpeg"``.
    """
    normalised = mime_hint.strip().lower()
    if normalised in _IMAGE_MIMES or normalised == _PDF_MIME:
        return normalised

    # Magic-byte sniffing fallback
    header = file_bytes[:8]
    if header[:4] == b"%PDF":
        return _PDF_MIME
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if header[:2] == b"\xff\xd8":
        return "image/jpeg"
    if header[:4] in {b"II*\x00", b"MM\x00*"}:
        return "image/tiff"
    if header[:2] == b"BM":
        return "image/bmp"

    logger.warning(
        "Aegis-Vision: Cannot determine file type from hint=%r or magic bytes. "
        "Attempting image decode as fallback.",
        mime_hint,
    )
    return "image/unknown"


# ── Public API ────────────────────────────────────────────────────────────────


def extract_text(file_bytes: bytes, mime_hint: str = "") -> OcrResult:
    """Extract plain text from an image or PDF byte-stream via OCR.

    This is the single entry-point for the Aegis-Vision module.  The
    orchestrator calls this **before** the scrubber so that all OCR
    output passes through the PII de-identification pipeline.

    Parameters
    ----------
    file_bytes:
        Raw bytes of the uploaded file.  Must not be empty.
    mime_hint:
        MIME type string from the HTTP Content-Type header or multipart
        metadata.  Used to skip magic-byte detection when already known.
        Pass an empty string to rely solely on magic-byte sniffing.

    Returns
    -------
    OcrResult
        Structured result containing the raw text, page count, latency,
        detected MIME type, and any non-fatal warnings.

    Raises
    ------
    OcrError
        For empty input, unreadable files, missing system dependencies,
        encrypted PDFs, or other unrecoverable extraction failures.

    Example
    -------
    .. code-block:: python

        with open("invoice.pdf", "rb") as f:
            pdf_bytes = f.read()

        result = extract_text(pdf_bytes, mime_hint="application/pdf")
        print(f"Extracted {result.page_count} page(s) in {result.ocr_latency_ms:.1f} ms")
        print(result.raw_text)
    """
    if not file_bytes:
        raise OcrError("Received empty file stream — nothing to extract.")

    effective_mime = _sniff_mime(file_bytes, mime_hint)
    logger.info(
        "Aegis-Vision: Dispatching OCR for MIME=%r (%d bytes)", effective_mime, len(file_bytes)
    )

    t_start = time.perf_counter()

    if effective_mime == _PDF_MIME:
        raw_text, page_count, warnings = _extract_from_pdf_bytes(file_bytes)
    else:
        # Treat anything non-PDF as an image; _extract_from_image_bytes
        # will raise OcrError if the bytes are not a valid image format.
        raw_text, warnings = _extract_from_image_bytes(file_bytes)
        page_count = 1

    ocr_latency_ms = (time.perf_counter() - t_start) * 1000

    logger.info(
        "Aegis-Vision: OCR complete — %d page(s), %.0f ms, %d warning(s).",
        page_count,
        ocr_latency_ms,
        len(warnings),
    )
    for w in warnings:
        logger.warning("Aegis-Vision: %s", w)

    return OcrResult(
        raw_text=raw_text,
        page_count=page_count,
        ocr_latency_ms=round(ocr_latency_ms, 2),
        mime_detected=effective_mime,
        warnings=warnings,
    )


def warm_reader() -> None:
    """Force-initialise the EasyOCR singleton at application startup.

    Call this from the FastAPI ``lifespan`` hook alongside
    ``warm_engines()`` (scrubber) so neither the OCR model nor the
    spaCy model is loaded lazily on the first real request.

    Example
    -------
    .. code-block:: python

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await initialize_db()
            warm_engines()   # Presidio / spaCy
            warm_reader()    # EasyOCR
            yield
    """
    _ReaderSingleton.get()._ensure_reader()
