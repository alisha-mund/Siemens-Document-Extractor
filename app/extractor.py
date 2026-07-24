"""
Document extraction pipeline.

Architecture:
  1. pdfplumber  → digital text layer (order details, line items, grand total)
  2. PyMuPDF     → extract embedded images from the PDF
  3. tesseract   → OCR on embedded images (supplier confirmation stamp)
  4. regex       → structured parsing of both text sources
  5. validation  → arithmetic cross-checks on extracted values

Why this approach:
  - The top section of the document is digital text — an LLM or VLM would add
    latency, cost, and hallucination risk for no benefit over direct text parsing.
  - The bottom section is a scanned image embedded in the PDF. pdfplumber can't
    read it, so we fall back to OCR. This mirrors real-world documents where
    digital and scanned content coexist.
  - Confidence scores reflect the extraction method: digital text gets 0.97
    (high but not 1.0 — encoding or layout edge cases exist), OCR gets scores
    based on tesseract's own word-level confidence.
"""
import pytesseract
import platform
if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
import io
import re
import logging
from datetime import datetime
from typing import Optional

import pdfplumber
import fitz  # PyMuPDF

from PIL import Image

from app.schemas import (
    ExtractedField, OrderDetails, LineItem, SupplierConfirmation,
    Validation, ExtractionResult,
)

logger = logging.getLogger(__name__)

# Confidence baselines — digital text is reliable, OCR varies
DIGITAL_CONFIDENCE = 0.97
OCR_BASE_CONFIDENCE = 0.85


def extract_document(pdf_bytes: bytes) -> ExtractionResult:
    """
    Main entry point. Takes raw PDF bytes, returns structured extraction.
    """
    warnings = []
    methods_used = []

    # --- Step 1: Digital text extraction ---
    raw_text = _extract_digital_text(pdf_bytes)
    methods_used.append("pdfplumber")

    if not raw_text or len(raw_text.strip()) < 20:
        raise ValueError("PDF contains no extractable text — may be a fully scanned document")

    # --- Step 2: Parse order details from digital text ---
    order_details = _parse_order_details(raw_text)

    # --- Step 3: Parse line items from digital text ---
    line_items = _parse_line_items(raw_text)
    if not line_items:
        warnings.append("No line items found in document")

    # --- Step 4: Parse grand total ---
    grand_total_field = _parse_grand_total(raw_text)

    # --- Step 5: Extract and OCR embedded images ---
    supplier_confirmation = None
    images = _extract_images(pdf_bytes)

    if images:
        methods_used.append("tesseract_ocr")
        for img in images:
            ocr_text, mean_conf = _ocr_image(img)
            parsed = _parse_supplier_confirmation(ocr_text, mean_conf)
            if parsed:
                supplier_confirmation = parsed
                break
    else:
        warnings.append("No embedded images found — supplier confirmation may be missing")

    # --- Step 6: Cross-validation ---
    computed_sum = sum(item.total.value for item in line_items)
    all_lines_ok = all(item.line_valid for item in line_items)

    # Round to avoid floating-point drift
    totals_match = abs(computed_sum - grand_total_field.value) < 0.01

    if not totals_match:
        warnings.append(
            f"Grand total mismatch: stated {grand_total_field.value}, "
            f"computed {computed_sum}"
        )

    validation = Validation(
        grand_total_stated=grand_total_field,
        grand_total_computed=round(computed_sum, 2),
        totals_consistent=totals_match,
        all_lines_valid=all_lines_ok,
    )

    return ExtractionResult(
        extraction_timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        extraction_methods=methods_used,
        order_details=order_details,
        line_items=line_items,
        supplier_confirmation=supplier_confirmation,
        validation=validation,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_digital_text(pdf_bytes: bytes) -> str:
    """Extract selectable text from all pages using pdfplumber."""
    full_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text.append(text)
    return "\n".join(full_text)


# ---------------------------------------------------------------------------
# Image extraction and OCR
# ---------------------------------------------------------------------------

def _extract_images(pdf_bytes: bytes) -> list:
    """Pull embedded images out of the PDF using PyMuPDF."""
    images = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                pil_image = Image.open(io.BytesIO(image_bytes))
                images.append(pil_image)
    finally:
        doc.close()
    return images


def _ocr_image(image: Image.Image) -> tuple[str, float]:
    """
    Run tesseract OCR on a PIL image.
    Returns (extracted_text, mean_confidence).
    """
    text = pytesseract.image_to_string(image)

    # Get word-level confidence scores
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    confidences = [c for c in data["conf"] if c > 0]
    mean_conf = sum(confidences) / len(confidences) / 100.0 if confidences else 0.5

    return text, mean_conf


# ---------------------------------------------------------------------------
# Order details parsing
# ---------------------------------------------------------------------------

# Map of label patterns to field names
ORDER_FIELD_MAP = {
    r"PO\s*Number": "po_number",
    r"PO\s*Date": "po_date",
    r"Buyer": "buyer",
    r"Supplier\s*ID": "supplier_id",
    r"Supplier": "supplier_name",  # must come after Supplier ID
    r"Currency": "currency",
    r"Payment\s*Terms": "payment_terms",
    r"Incoterms": "incoterms",
}


def _parse_order_details(text: str) -> OrderDetails:
    """Parse the key-value pairs from the Order Details section."""
    fields = {}

    for pattern, field_name in ORDER_FIELD_MAP.items():
        match = re.search(rf"{pattern}\s*:\s*(.+)", text)
        if match:
            value = match.group(1).strip()
            fields[field_name] = value

    # Parse buyer into name and department
    buyer_raw = fields.get("buyer", "")
    buyer_name = buyer_raw
    buyer_dept = ""
    if "—" in buyer_raw:
        parts = buyer_raw.split("—", 1)
        buyer_name = parts[0].strip()
        buyer_dept = parts[1].strip()
    elif "–" in buyer_raw:
        parts = buyer_raw.split("–", 1)
        buyer_name = parts[0].strip()
        buyer_dept = parts[1].strip()

    # Normalize date
    po_date_raw = fields.get("po_date", "")
    po_date_iso = _parse_date(po_date_raw)

    return OrderDetails(
        po_number=_field(fields.get("po_number", ""), DIGITAL_CONFIDENCE, "digital_text"),
        po_date=_field(po_date_iso or po_date_raw, DIGITAL_CONFIDENCE, "digital_text"),
        buyer_name=_field(buyer_name, DIGITAL_CONFIDENCE, "digital_text"),
        buyer_department=_field(buyer_dept, DIGITAL_CONFIDENCE, "digital_text"),
        supplier_name=_field(fields.get("supplier_name", ""), DIGITAL_CONFIDENCE, "digital_text"),
        supplier_id=_field(fields.get("supplier_id", ""), DIGITAL_CONFIDENCE, "digital_text"),
        currency=_field(fields.get("currency", ""), DIGITAL_CONFIDENCE, "digital_text"),
        payment_terms=_field(fields.get("payment_terms", ""), DIGITAL_CONFIDENCE, "digital_text"),
        incoterms=_field(fields.get("incoterms", ""), DIGITAL_CONFIDENCE, "digital_text"),
    )


# ---------------------------------------------------------------------------
# Line items parsing
# ---------------------------------------------------------------------------

def _parse_line_items(text: str) -> list[LineItem]:
    """Parse the line items table from extracted text."""
    items = []

    # Match: position, description (greedy until trailing numbers), qty, unit_price, total
    pattern = re.compile(
        r"^(\d+)\s+(.+?)\s+(\d+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})$",
        re.MULTILINE,
    )

    for match in pattern.finditer(text):
        pos = int(match.group(1))
        desc = match.group(2).strip()
        qty = int(match.group(3))
        unit_price = _parse_number(match.group(4))
        total = _parse_number(match.group(5))

        # Cross-check: does qty * unit_price == total?
        expected = round(qty * unit_price, 2)
        line_ok = abs(expected - total) < 0.01

        items.append(LineItem(
            position=pos,
            description=_field(desc, DIGITAL_CONFIDENCE, "digital_text"),
            quantity=_field(qty, DIGITAL_CONFIDENCE, "digital_text"),
            unit_price=_field(unit_price, DIGITAL_CONFIDENCE, "digital_text"),
            total=_field(total, DIGITAL_CONFIDENCE, "digital_text"),
            line_valid=line_ok,
        ))

    return items


def _parse_grand_total(text: str) -> ExtractedField:
    """Extract the grand total value."""
    match = re.search(r"Grand\s*Total\s*:\s*([\d,]+\.\d{2})", text)
    if match:
        return _field(_parse_number(match.group(1)), DIGITAL_CONFIDENCE, "digital_text")
    return _field(0.0, 0.0, "digital_text")


# ---------------------------------------------------------------------------
# Supplier confirmation parsing (from OCR output)
# ---------------------------------------------------------------------------

SUPPLIER_FIELD_MAP = {
    r"Approved\s*By": "approved_by",
    r"Approval\s*Role": "approval_role",
    r"Approval\s*Date": "approval_date",
    r"Confirmed\s*Delivery\s*Date": "confirmed_delivery_date",
    r"Warranty\s*Period": "warranty_period",
    r"Serial\s*Number": "serial_number",
    r"Inspection\s*Result": "inspection_result",
    r"Special\s*Note": "special_note",
}


def _parse_supplier_confirmation(
    ocr_text: str, mean_conf: float
) -> Optional[SupplierConfirmation]:
    """Parse key-value pairs from OCR text of the supplier stamp."""
    if not ocr_text or "SUPPLIER CONFIRMATION" not in ocr_text.upper():
        return None

    fields = {}
    for pattern, field_name in SUPPLIER_FIELD_MAP.items():
        match = re.search(rf"{pattern}\s*:\s*(.+)", ocr_text, re.IGNORECASE)
        if match:
            fields[field_name] = match.group(1).strip()

    # Clamp OCR confidence — tesseract can be overconfident
    conf = min(mean_conf, OCR_BASE_CONFIDENCE + 0.10)

    # Normalize dates from OCR
    approval_date_raw = fields.get("approval_date", "")
    delivery_date_raw = fields.get("confirmed_delivery_date", "")

    # Detect signature presence
    sig_present = bool(re.search(r"Signature\s*:", ocr_text, re.IGNORECASE))

    return SupplierConfirmation(
        approved_by=_field(fields.get("approved_by", ""), conf, "ocr"),
        approval_role=_field(fields.get("approval_role", ""), conf, "ocr"),
        approval_date=_field(
            _parse_date(approval_date_raw) or approval_date_raw, conf, "ocr"
        ),
        confirmed_delivery_date=_field(
            _parse_date(delivery_date_raw) or delivery_date_raw, conf, "ocr"
        ),
        warranty_period=_field(fields.get("warranty_period", ""), conf, "ocr"),
        serial_number=_field(fields.get("serial_number", ""), conf, "ocr"),
        inspection_result=_field(fields.get("inspection_result", ""), conf, "ocr"),
        special_note=_field(fields.get("special_note", ""), conf, "ocr") if fields.get("special_note") else None,
        signature_present=_field(sig_present, conf * 0.9, "ocr"),
    )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _field(value, confidence: float, source: str) -> ExtractedField:
    """Shorthand for creating an ExtractedField."""
    return ExtractedField(value=value, confidence=round(confidence, 2), source=source)


def _parse_date(date_str: str) -> Optional[str]:
    """Try to parse a human-readable date into ISO 8601 format."""
    formats = [
        "%d %B %Y",      # 10 July 2026
        "%d %b %Y",      # 10 Jul 2026
        "%B %d, %Y",     # July 10, 2026
        "%Y-%m-%d",      # already ISO
        "%d/%m/%Y",      # 10/07/2026
        "%d.%m.%Y",      # 10.07.2026
    ]
    clean = date_str.strip()
    for fmt in formats:
        try:
            parsed = datetime.strptime(clean, fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_number(num_str: str) -> float:
    """Parse a formatted number string (e.g., '12,500.00') into a float."""
    return float(num_str.replace(",", ""))
