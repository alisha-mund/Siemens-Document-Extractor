"""
Document extraction microservice.

Single endpoint: POST /extract — accepts a PDF, returns structured JSON.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from app.extractor import extract_document
from app.schemas import ExtractionResult, ErrorResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Max upload size: 20 MB — purchase orders shouldn't be larger
MAX_FILE_SIZE = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Document extraction service starting up")
    yield
    logger.info("Service shutting down")


app = FastAPI(
    title="Document Extraction Service",
    description=(
        "Extracts structured metadata from business documents (purchase orders, "
        "supplier confirmations). Returns typed JSON with confidence scores and "
        "provenance tracking for every field."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health_check():
    """Liveness probe for container orchestration."""
    return {"status": "healthy"}


@app.post(
    "/extract",
    response_model=ExtractionResult,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid input"},
        422: {"model": ErrorResponse, "description": "Document could not be processed"},
        500: {"model": ErrorResponse, "description": "Internal extraction error"},
    },
)
async def extract_endpoint(file: UploadFile = File(..., description="PDF document to extract")):
    """
    Upload a PDF document and receive structured metadata as JSON.

    The service uses two extraction strategies:
    - Digital text parsing (pdfplumber) for machine-readable content
    - OCR (tesseract) for embedded scanned images

    Every field in the response includes a confidence score and a source
    indicator so downstream systems know how much to trust each value.
    """

    # --- Input validation ---
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail=f"Expected a PDF file, got '{file.filename}'. Only PDF is supported.",
        )

    content_type = file.content_type or ""
    if content_type and content_type != "application/pdf":
        logger.warning(f"Unexpected content type: {content_type} for {file.filename}")

    # Read file with size check
    pdf_bytes = await file.read()

    if len(pdf_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(pdf_bytes)} bytes). Maximum is {MAX_FILE_SIZE} bytes.",
        )

    # Quick PDF magic-byte check
    if not pdf_bytes[:5] == b"%PDF-":
        raise HTTPException(
            status_code=400,
            detail="File does not appear to be a valid PDF (missing PDF header)",
        )

    # --- Extraction ---
    try:
        logger.info(f"Processing document: {file.filename} ({len(pdf_bytes)} bytes)")
        result = extract_document(pdf_bytes)
        logger.info(
            f"Extraction complete: {len(result.line_items)} line items, "
            f"supplier_confirmation={'yes' if result.supplier_confirmation else 'no'}, "
            f"warnings={len(result.warnings)}"
        )
        return result

    except ValueError as e:
        logger.error(f"Extraction failed (bad input): {e}")
        raise HTTPException(status_code=422, detail=str(e))

    except Exception as e:
        logger.exception(f"Unexpected extraction error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal error during document extraction. Check server logs.",
        )
