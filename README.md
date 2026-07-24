# Document Extraction Service

A Python microservice that reads a purchase order PDF and returns structured metadata as JSON. Built with FastAPI, pdfplumber, and tesseract OCR.

## Quick Start

```bash
# Build
docker build -t doc-extractor .

# Run
docker run -p 8000:8000 doc-extractor

# Extract a document
curl -X POST http://localhost:8000/extract \
  -F "file=@sample-document_purchase-order.pdf"
```

The service is also accessible at `http://localhost:8000/docs` for interactive API docs.

---

## Decisions I Made and Why

**Hybrid extraction instead of a single technique.**
The sample document has two fundamentally different sections — digital text at the top (order details, line items) and a scanned image at the bottom (supplier confirmation stamp). Using one tool for both would mean either overcomplicating the easy part or failing the hard part. So I split it:
- **pdfplumber** for the digital text. It's deterministic, fast, and doesn't hallucinate. There's no reason to send structured text through an LLM when regex does the job reliably.
- **tesseract OCR** for the embedded image. The supplier stamp is a scanned image — pdfplumber can't see it at all. PyMuPDF extracts the image, tesseract reads it.

This two-path approach is something I worked with directly in my thesis, where I had scanned historical documents that mixed machine-readable text with handwritten annotations. The lesson I took from that: use the simplest tool that works for each section, and track which tool produced each field.

**No LLM in the extraction loop.**
I considered calling an LLM API to structure the output, but decided against it for this use case:
- The document layout is consistent enough that regex-based parsing works.
- An LLM adds network latency, API cost per request, and a hallucination risk that's hard to test for.
- For a service that Siemens would run in production on thousands of documents, deterministic extraction with known failure modes is easier to trust than a probabilistic one.

That said — if the documents were more varied (different suppliers, different layouts, handwritten fields), an LLM would start making sense. I'd add it as a configurable fallback, not the primary path.

**Confidence scores and provenance on every field.**
Every extracted value carries a `confidence` score and a `source` tag (`digital_text`, `ocr`, or `computed`). The idea is that a downstream system — say, an ERP import — can decide its own threshold: auto-accept fields above 0.95, flag fields below 0.85 for human review.

Digital text gets 0.97 (high but not 1.0, because even text extraction has edge cases with unusual PDF encodings). OCR gets the mean of tesseract's word-level confidence, capped at 0.95.

**Arithmetic cross-validation.**
The service checks that `quantity × unit_price == total` for every line item, and that the sum of line totals matches the stated grand total. If there's a mismatch, it shows up in the `validation` section and as a warning. This catches both extraction errors and errors in the source document.

**Production-oriented Dockerfile.**
Multi-stage build (builder layer installs pip packages, runtime layer only has tesseract + Python + the app). Runs as a non-root user. Has a health check endpoint. Single uvicorn worker. For production it would scale horizontally with multiple containers behind a load balancer, not vertically with multiple workers inside one container.

---

## What I Did NOT Do 

**The regex parsing is brittle.**
It works on this document. But if the label formatting changes — "PO Number" becomes "Purchase Order No." or the line items table uses tabs instead of spaces, the regex breaks silently. In production, I'd want a test suite with 20+ document variants to catch these edge cases, and probably a schema-driven parser instead of hardcoded patterns.

**OCR quality depends on image quality.**
Tesseract gave 94% mean confidence on this clean scanned image. On a photo of a faxed document, or a stamp that's been photocopied three times, it would be much worse. I'm not doing any image preprocessing (deskewing, contrast adjustment, denoising) that would help with degraded inputs. That would be a priority improvement.

**No async processing.**
The endpoint processes the document synchronously. For a single purchase order, this is fine (~1 second). But if someone uploads a 50-page inspection report, the request would block. A production version would need a task queue, with a job ID that the client polls for results.

**Only one document type.**
The service assumes every upload is a purchase order. A real system would need a document classifier first, "is this a PO, an invoice, a supplier confirmation?" — and route to the appropriate extraction schema. I structured the code so that adding new document types would mean adding new parser functions and schemas, not rewriting the pipeline.

**No integration tests with the Docker image.**
I tested the extraction logic and the API locally but didn't write automated tests that build the Docker image and run the full pipeline inside it. In a real project, that's part of the CI/CD pipeline.

**The "ClassA" OCR artifact.**
Tesseract reads "Class A" as "ClassA" (missing space). I could add post-processing rules for known OCR artifacts, but that just adds more complexity for every pattern. The better would probably be an LLM-based cleanup step: run the OCR output through a small model to fix obvious errors. But the current implementation doesnot have that layer yet.
---

## Example Output

See `example_output.json` for the full JSON response from the sample document.

## Project Structure

```
├── app/
│   ├── main.py          # FastAPI application and endpoint
│   ├── extractor.py     # Extraction pipeline (pdfplumber + OCR)
│   └── schemas.py       # Pydantic response models
├── Dockerfile           # Multi-stage production build
├── requirements.txt     # Pinned dependencies
└── example_output.json  # Sample output
```
