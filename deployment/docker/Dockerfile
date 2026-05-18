# =============================================================================
# Quant Trading System — Multi-stage Dockerfile
# Stage 1: Builder (compile deps)
# Stage 2: Production (minimal runtime)
# =============================================================================

FROM python:3.11-slim-bookworm AS builder

# System build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
ENV POETRY_HOME=/opt/poetry
RUN curl -sSL https://install.python-poetry.org | python3 - --version 2.4.1
ENV PATH="$POETRY_HOME/bin:$PATH"

WORKDIR /app

# Copy dependency files first (layer caching)
COPY pyproject.toml poetry.lock ./

# Install dependencies (without dev)
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi --no-root

# ── Production stage ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS production

LABEL org.opencontainers.image.title="Quant Trading System"
LABEL org.opencontainers.image.description="Production-grade multi-market quantitative trading system"
LABEL org.opencontainers.image.version="0.1.0"
LABEL org.opencontainers.image.licenses="MIT"

# Runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash quant

WORKDIR /app

# Copy installed deps from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=quant:quant . .

# Switch to non-root user
USER quant

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

ENTRYPOINT ["quant-cli"]
CMD ["--help"]
