#!/usr/bin/env bash
# Runs ON the server inside /opt/nexus: build the image, (re)start the stack,
# wait for the API to be healthy, print where it is served.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -s .env ]]; then
  echo "ERROR: .env missing — copy .env.production.example to .env and fill it in." >&2
  exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a
: "${DOMAIN:?DOMAIN must be set in .env}"

echo "==> docker compose up"
docker compose pull redis caddy
docker compose build api
docker compose up -d --remove-orphans

echo "==> Waiting for the API to report healthy"
for i in $(seq 1 60); do
  status=$(docker inspect -f '{{.State.Health.Status}}' nexus-api-1 2>/dev/null || echo starting)
  if [[ "$status" == "healthy" ]]; then break; fi
  sleep 5
done
docker compose ps

echo "==> Local health"
docker compose exec -T api curl -fsS http://127.0.0.1:8000/health && echo

echo "==> Public health (HTTPS via Caddy; the first run may take ~30 s to issue the certificate)"
for i in $(seq 1 12); do
  if curl -fsS "https://${DOMAIN}/health" 2>/dev/null; then echo; break; fi
  sleep 5
done

echo "==> Pruning old images"
docker image prune -f >/dev/null

echo
echo "Done. API: https://${DOMAIN}   logs: docker compose logs -f api"
