#!/usr/bin/env bash
# Record golden fixtures from the local Lemmy/Ghost (and Discourse with --full) into tests/fixtures/recorded/.
# Runs the recorder inside the compose network via the bootstrap service image: no host ports are published.
# Usage: deploy/bootstrap/record_fixtures.sh [--full] [--down]
#   --full  also start the real Discourse (profile `full`) and record its fixtures
#   --down  `docker compose down -v` afterwards
# Compose project: $COMPOSE_PROJECT_NAME or ibx_p2a.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
project="${COMPOSE_PROJECT_NAME:-ibx_p2a}"
full=0; down=0
for a in "$@"; do
  case "$a" in
    --full) full=1 ;;
    --down) down=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
files=(-f "$repo/deploy/compose/lemmy.yaml" -f "$repo/deploy/compose/ghost.yaml"
       -f "$repo/deploy/compose/toxiproxy.yaml" -f "$repo/deploy/compose/bootstrap.yaml")
platforms="lemmy,ghost"
if [ "$full" = 1 ]; then
  files+=(-f "$repo/deploy/compose/discourse.yaml" --profile full)
  platforms="lemmy,ghost,discourse"
fi
out="$repo/tests/fixtures/recorded"
mkdir -p "$out"
cleanup() { if [ "$down" = 1 ]; then docker compose -p "$project" "${files[@]}" down -v >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT
docker compose -p "$project" "${files[@]}" up -d --wait
docker compose -p "$project" "${files[@]}" run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$out:/fixtures" \
  bootstrap python /ibx/bootstrap/record_fixtures.py --out /fixtures --platforms "$platforms"
echo "fixtures written to tests/fixtures/recorded/ ($platforms)"
