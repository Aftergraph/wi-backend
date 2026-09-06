FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . 2>/dev/null || pip install --no-cache-dir fastapi uvicorn pydantic

# Copy source
COPY src/ src/
COPY tests/ tests/

# Create data directory
RUN mkdir -p /data

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/healthz'); r.raise_for_status()" || exit 1

# Public containers always use the fail-closed security boundary.
CMD ["python", "-m", "uvicorn", "aftergraph_work_intelligence.secure_api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
