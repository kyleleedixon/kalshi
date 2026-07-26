# Target linux/arm64 for Oracle Cloud Always Free ARM VMs (Ampere A1).
# All runtime deps must have ARM wheels; numpy/scipy/pandas/psycopg[binary] do.
FROM --platform=linux/arm64 python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY kalshi_bias_engine ./kalshi_bias_engine
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# Non-root user
RUN useradd --create-home --uid 1001 kbe && chown -R kbe:kbe /app
USER kbe

# Local write-spool path (persisted via docker volume in prod)
ENV KBE_SPOOL_PATH=/app/spool.sqlite

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "kalshi_bias_engine.runtime.main"]
