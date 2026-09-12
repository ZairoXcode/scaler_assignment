"""FastAPI web application for DOCX PII detection and redaction.

Exposes a web UI for uploading .docx documents, running the existing
detection/redaction engine, and downloading the redacted result.
"""

import json
import logging
import os
import shutil
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Tuple

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from detector import PiiDetector
from redactor import PiiRedactor

# ---------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pii_web_app")

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
CONFIG_PATH = Path(os.environ.get("PII_CONFIG", "config.yaml"))
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
DOCX_ZIP_SIGNATURE = b"PK\x03\x04"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# Shared pre-warmed detector and redactor instances
_detector: Optional[PiiDetector] = None
_redactor: Optional[PiiRedactor] = None


def get_engine() -> Tuple[PiiDetector, PiiRedactor]:
    """Return initialized detector and redactor instances."""
    global _detector, _redactor
    if _detector is None:
        _detector = PiiDetector(CONFIG_PATH)
    if _redactor is None:
        _redactor = PiiRedactor(CONFIG_PATH)
    return _detector, _redactor


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-warm PII detection models on server startup."""
    logger.info("Initializing PII detection and redaction models...")
    get_engine()
    try:
        from ocr_processor import _get_reader
        _get_reader()
        logger.info("EasyOCR engine warmed up.")
    except Exception as e:
        logger.warning("EasyOCR warm-up skipped: %s", e)
    logger.info("Engine ready to process documents.")
    yield


# ---------------------------------------------------------------------
# FastAPI App Definition
# ---------------------------------------------------------------------
app = FastAPI(
    title="PII Redaction & Anonymization Engine",
    description="Web service for detecting and redacting PII in Microsoft Word (.docx) documents.",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

@app.get("/health")
def health_check():
    """Lightweight liveness check for monitoring and Render health probes."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Render the document upload and redaction web interface."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
    )


@app.post("/redact")
async def redact_docx(file: UploadFile = File(...)):
    """Process an uploaded .docx document, apply redactions, and return the redacted file.

    Temporary files are strictly used and removed after processing to prevent
    sensitive document data persistence.
    """
    filename = file.filename or ""

    # 1. Filename validation
    if not filename.lower().endswith(".docx"):
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Invalid file type. Only Microsoft Word (.docx) documents are supported."
            },
        )

    # 2. Read content and validate size
    try:
        content = await file.read()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"detail": "Failed to read uploaded file."},
        )

    if not content:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "The uploaded file is empty. Please upload a valid .docx document."
            },
        )

    if len(content) > MAX_FILE_SIZE_BYTES:
        return JSONResponse(
            status_code=413,
            content={
                "detail": f"File exceeds maximum allowed size ({MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB)."
            },
        )

    # 3. Magic bytes validation (DOCX is a ZIP container starting with PK\x03\x04)
    if not content.startswith(DOCX_ZIP_SIGNATURE):
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Invalid .docx format. The file is corrupted or not a valid Microsoft Word document."
            },
        )

    # 4. Safe temporary execution
    detector_inst, redactor_inst = get_engine()

    temp_dir = tempfile.mkdtemp(prefix="pii_redact_")
    try:
        temp_input = Path(temp_dir) / "input.docx"
        temp_output = Path(temp_dir) / "output.docx"

        with open(temp_input, "wb") as f_in:
            f_in.write(content)

        total_redactions, audits = redactor_inst.redact_document(
            docx_path=temp_input,
            output_path=temp_output,
            detector=detector_inst,
            config_path=CONFIG_PATH,
        )

        if not temp_output.exists():
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Redaction pipeline failed to produce an output document."
                },
            )

        # Isolated OCR post-pass: detect and redact PII inside embedded images
        try:
            import docx as _docx
            from ocr_processor import process_embedded_images
            _ocr_doc = _docx.Document(str(temp_output))
            ocr_count = process_embedded_images(_ocr_doc, detector_inst)
            if ocr_count > 0:
                _ocr_doc.save(str(temp_output))
                total_redactions += ocr_count
        except Exception as ocr_err:
            logger.warning("OCR post-pass failed or skipped: %s", ocr_err)

        with open(temp_output, "rb") as f_out:
            output_bytes = f_out.read()

        category_counts = dict(Counter(audit.category for audit in audits))

        # Determine download filename
        base_name = Path(filename).name
        out_filename = (
            f"Redacted_{base_name}"
            if base_name.lower().endswith(".docx")
            else "Redacted_Document.docx"
        )

        headers = {
            "Content-Disposition": f'attachment; filename="{out_filename}"',
            "X-Total-Redactions": str(total_redactions),
            "X-Category-Counts": json.dumps(category_counts),
            "Access-Control-Expose-Headers": "X-Total-Redactions, X-Category-Counts, Content-Disposition",
        }

        return Response(
            content=output_bytes,
            media_type=DOCX_MEDIA_TYPE,
            headers=headers,
        )

    except Exception as exc:
        logger.error(
            "Document redaction failed with exception: %s",
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Could not process document. Ensure the file is a valid, uncorrupted .docx document."
            },
        )
    finally:
        # Guarantee removal of all temporary files
        shutil.rmtree(temp_dir, ignore_errors=True)
