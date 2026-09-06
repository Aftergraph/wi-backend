FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AFTERGRAPH_DB=/data/aftergraph-work-intelligence.db

WORKDIR /app

# Copy the package source before installing it. The old image attempted an
# editable install before src/ existed and silently fell back to a partial
# dependency-only install.
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir . \
    && addgroup --system aftergraph \
    && adduser --system --ingroup aftergraph --no-create-home aftergraph \
    && mkdir -p /data \
    && chown -R aftergraph:aftergraph /data

USER aftergraph

EXPOSE 8000

# Keep the health check inside the production dependency set by using only the
# Python standard library.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).read()" || exit 1

# Public containers always use the fail-closed security boundary.
CMD ["python", "-m", "uvicorn", "aftergraph_work_intelligence.secure_api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
