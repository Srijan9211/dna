FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Add src to Python path so 'dna' package is importable
ENV PYTHONPATH="/app/src:${PYTHONPATH}"

# Run as an unprivileged user.  Uploaded note attachments are written under
# ATTACHMENT_STORE_DIR (default /tmp/dna_attachments), which this user can create.
RUN useradd --system --create-home --uid 10001 dna && chown -R dna:dna /app
USER dna

# Expose port
EXPOSE 8000

# Liveness: /health answers without touching the production tracker.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \n    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Run the application
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
