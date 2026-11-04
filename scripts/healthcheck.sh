#!/usr/bin/env bash
# Quick health report of a running stack, from the Docker host.
#   ./scripts/healthcheck.sh            (panel on 127.0.0.1:8099, password from .env)
set -uo pipefail
PANEL="${PANEL:-http://127.0.0.1:${PANEL_PORT:-8099}}"
if [[ -f .env && -z "${PANEL_PASSWORD:-}" ]]; then
  PANEL_PASSWORD="$(grep -E '^PANEL_PASSWORD=' .env | cut -d= -f2-)"
fi
ok=0
echo "== containers"; docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}' || ok=1
echo "== panel"; curl -fsS "$PANEL/healthz" && echo || { echo "panel unreachable"; ok=1; }
echo "== backends"
curl -fsS -H "Authorization: Bearer ${PANEL_PASSWORD:-}" "$PANEL/api/status" | python3 -m json.tool || ok=1
for svc in llama-gpu llama-cpu; do
  if docker compose ps --status running --services 2>/dev/null | grep -qx "$svc"; then
    echo "== $svc"; docker compose exec -T "$svc" curl -fsS http://127.0.0.1:8080/health && echo || ok=1
  fi
done
if command -v nvidia-smi >/dev/null; then
  echo "== GPU"; nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total --format=csv
fi
exit $ok
