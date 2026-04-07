# Stage 1: dependency installation
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock* ./

# Install dependencies into /app/.venv (no project, just deps)
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY src/ ./src/

# Install the project itself
RUN uv sync --frozen --no-dev

# Stage 2: runtime image
FROM python:3.12-slim AS runtime

WORKDIR /app

# Create non-root user
RUN addgroup --system --gid 1001 gateway && \
    adduser --system --uid 1001 --gid 1001 gateway

# Copy venv and source from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Make venv binaries available
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"

USER gateway

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
