# SecureRAG++ / Nexus API — production image.
#
# Works on x86_64 and arm64 (Oracle Cloud A1 "Always Free" VMs are arm64).
# Chromium is installed for the crawl4ai scraper, which is why this image is
# ~1.5 GB; everything else is a plain FastAPI/uvicorn app.
#
#   docker build -t nexus-api .
#   docker run --env-file .env -p 8000:8000 nexus-api
#
# In production use docker-compose.yml (adds Redis + Caddy for HTTPS).

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Browsers live outside the user's home so the non-root runtime user
    # can use the copies installed at build time.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# curl: container healthcheck. ca-certificates/tzdata: outbound HTTPS + sane
# timestamps. The Chromium runtime libraries are pulled in by
# `playwright install --with-deps` below (it runs apt itself).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code changes don't invalidate this (slow) layer.
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache

# Application code (see .dockerignore for what stays out).
COPY . .

# Non-root runtime user. storage/ is the only path the app writes to
# (temporary upload staging — real files go to Cloudinary).
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/storage/uploads \
 && chown -R app:app /app /ms-playwright
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# One worker on purpose: the app runs its background sweeps (indexing
# recovery, conversation/audit retention) as in-process asyncio tasks, so a
# second worker would run them twice. Scale by adding instances behind
# Caddy, not by adding workers. --proxy-headers trusts X-Forwarded-* from
# the Caddy container so client IPs reach the rate limiter and audit log.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "75", \
     "--no-server-header"]
