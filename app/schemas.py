"""
Response schemas for the document extraction service.

Design decisions:
- Every extracted field carries confidence + source metadata so downstream
  systems can decide how much to trust each value.
- Dates are ISO 8601 strings, prices are floats, quantities are ints.
- The supplier_confirmation section is Optional because it comes from OCR
  on an embedded image — if the image is missing or unreadable, we still
  return everything else rather than failing the whole request.
- Validation section cross-checks arithmetic so a consumer doesn't have to.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Any


class ExtractedField(BaseModel):
    """A single extracted value with extraction metadata."""
    value: Any
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="How confident the extraction is. 0.95+ for digital text, lower for OCR."
    )
    source: str = Field(
        description="How this value was extracted: 'digital_text', 'ocr', or 'computed'"
    )


class OrderDetails(BaseModel):
    po_number: ExtractedField
    po_date: ExtractedField
    buyer_name: ExtractedField
    buyer_department: ExtractedField
    supplier_name: ExtractedField
    supplier_id: ExtractedField
    currency: ExtractedField
    payment_terms: ExtractedField
    incoterms: ExtractedField


class LineItem(BaseModel):
    position: int
    description: ExtractedField
    quantity: ExtractedField
    unit_price: ExtractedField
    total: ExtractedField
    line_valid: bool = Field(
        description="True if quantity * unit_price == total (arithmetic cross-check)"
    )


class SupplierConfirmation(BaseModel):
    approved_by: ExtractedField
    approval_role: ExtractedField
    approval_date: ExtractedField
    confirmed_delivery_date: ExtractedField
    warranty_period: ExtractedField
    serial_number: ExtractedField
    inspection_result: ExtractedField
    special_note: Optional[ExtractedField] = None
    signature_present: ExtractedField


class Validation(BaseModel):
    grand_total_stated: ExtractedField
    grand_total_computed: float = Field(
        description="Sum of all line item totals, computed by the service"
    )
    totals_consistent: bool = Field(
        description="True if stated grand total matches computed sum"
    )
    all_lines_valid: bool = Field(
        description="True if every line item passes qty * unit_price == total"
    )


class ExtractionResult(BaseModel):
    document_type: str = "purchase_order"
    extraction_timestamp: str = Field(description="ISO 8601 timestamp of extraction")
    extraction_methods: List[str] = Field(
        description="List of techniques used: e.g. ['pdfplumber', 'tesseract_ocr']"
    )
    order_details: OrderDetails
    line_items: List[LineItem]
    supplier_confirmation: Optional[SupplierConfirmation] = None
    validation: Validation
    warnings: List[str] = Field(
        default_factory=list,
        description="Any issues encountered during extraction"
    )


class ErrorResponse(BaseModel):
    error: str
    detail: str
