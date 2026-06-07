# mochi-carry-signal production image. Mirrors the position-manager's Dockerfile
# (python:3.11-slim, uvicorn) but serves the signal app on :8100.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is for the container HEALTHCHECK below. (No build toolchain needed: all
# pinned deps ship manylinux wheels for cp311.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && rm -rf /var/lib/apt/lists/*

# Install from pyproject (the single source of truth for pinned deps). README is
# referenced by pyproject's `readme=`, and package-data ships templates/*.html.
COPY pyproject.toml README.md ./
COPY mochi_carry_signal ./mochi_carry_signal
RUN pip install --upgrade pip && pip install .

RUN mkdir -p /app/data

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8100/healthz || exit 1

CMD ["uvicorn", "mochi_carry_signal.web:app", "--host", "0.0.0.0", "--port", "8100"]
