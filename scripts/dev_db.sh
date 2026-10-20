#!/usr/bin/env bash
# A throwaway Postgres 16 for local development on a random free host port (never 5433/5434).
#   scripts/dev_db.sh up      start it, apply migrations, print DATABASE_URL
#   scripts/dev_db.sh down    remove it (and its data)
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="${IBX_DEV_DB_NAME:-ibx_dev_db}"
case "${1:-up}" in
  up)
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      docker run -d --rm --name "$NAME" -e POSTGRES_USER=ibx -e POSTGRES_PASSWORD=ibx-dev-password -e POSTGRES_DB=ibx \
        -p 127.0.0.1::5432 postgres:16-alpine >/dev/null
    fi
    until docker exec "$NAME" pg_isready -U ibx -d ibx >/dev/null 2>&1; do sleep 1; done
    PORT="$(docker port "$NAME" 5432/tcp | head -1 | awk -F: '{print $NF}')"
    URL="postgresql+asyncpg://ibx:ibx-dev-password@127.0.0.1:${PORT}/ibx"
    uv run python -m app.db.migrate --url "$URL" >/dev/null
    echo "DATABASE_URL=$URL"
    ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 || true; echo "removed $NAME" ;;
  *) echo "usage: $0 [up|down]" >&2; exit 2 ;;
esac
