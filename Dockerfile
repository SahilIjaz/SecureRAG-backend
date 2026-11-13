# SecureRAG++ / Nexus API — production image.
#
# Works on x86_64 and arm64. Chromium (for the crawl4ai website scraper) is
# installed by default and is what makes the image ~1.5 GB; on small hosts
# (Render free tier, 512 MB) build with --build-arg INSTALL_CHROMIUM=false
# and set CRAWL4AI_ENABLED=false — everything except URL scraping works.
#
#   docker build -t nexus-api .
#   docker run --env-file .env -p 8000:8000 nexus-api
#
# In production use docker-compose.yml (VM) or render.yaml (Render).

FROM python:3.12-slim-bookworm

ARG INSTALL_CHROMIUM=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Browsers live outside the user's home so the non-root runtime user
    # can use the copies installed at build time.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # Port to listen on; hosts like Render inject their own PORT.
    PORT=8000

# curl: container healthcheck. ca-certificates/tzdata: outbound HTTPS + sane
# timestamps. The Chromium runtime libraries are pulled in by
# `playwright install --with-deps` below (it runs apt itself).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code changes don't invalidate this (slow) layer.
COPY requirements.txt requirements-post.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 && pip install --no-deps -r requirements-post.txt \
 && if [ "$INSTALL_CHROMIUM" = "true" ]; then \
      playwright install --with-deps chromium; \
    else \
      echo "Skipping Chromium (INSTALL_CHROMIUM=$INSTALL_CHROMIUM) — set CRAWL4AI_ENABLED=false"; \
      mkdir -p /ms-playwright; \
    fi \
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
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# One worker on purpose: the app runs its background sweeps (indexing
# recovery, conversation/audit retention) as in-process asyncio tasks, so a
# second worker would run them twice. Scale by adding instances behind the
# proxy, not by adding workers. --proxy-headers trusts X-Forwarded-* from
# the reverse proxy (Caddy / Render) so client IPs reach the rate limiter
# and the audit log. Shell form so ${PORT} expands.
CMD exec uvicorn app.main:app \
     --host 0.0.0.0 --port "${PORT}" \
     --workers 1 \
     --proxy-headers --forwarded-allow-ips "*" \
     --timeout-keep-alive 75 \
     --no-server-header
