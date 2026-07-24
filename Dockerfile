# ---------------------------------------------------------------------------
# Multi-stage build for a production document extraction service
#
# Stage 1: install Python dependencies in a builder layer
# Stage 2: slim runtime with only tesseract + Python + app code
#
# Why multi-stage:
#   - Build tools (gcc, pip cache) stay out of the final image
#   - Final image is ~400 MB instead of ~1.2 GB
#   - Smaller attack surface for security scanning
# ---------------------------------------------------------------------------

# --- Stage 1: Builder ---
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Stage 2: Runtime ---
FROM python:3.11-slim

# Install tesseract OCR — required for extracting scanned image content.
# We use the slim base and only add what we need.
RUN apt-get update && \
    apt-get install -y --no-install-recommends tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create a non-root user — never run production services as root
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy application code
COPY app/ ./app/

# Switch to non-root user
USER appuser

# Expose the service port
EXPOSE 8000

# Health check — container orchestrators use this to know if the service is alive
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Running with uvicorn as a single worker is fine for this service.
# In production behind a load balancer, you'd scale horizontally (multiple containers)
# rather than adding workers inside one container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
