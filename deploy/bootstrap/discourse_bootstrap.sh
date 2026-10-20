#!/usr/bin/env bash
# Runs inside the official Discourse image (service `discourse-bootstrap`, profile `full`).
# Calls discourse_bootstrap.rb via `rails runner`, writes $BOOTSTRAP_OUT_DIR/discourse.json, verifies the API key
# against the running `discourse` service, then idles so its healthcheck can report readiness. Prints no secrets.
set -euo pipefail
out="${BOOTSTRAP_OUT_DIR:-/bootstrap/out}"
base="${IBX_DISCOURSE_URL:-http://discourse}"
mkdir -p "$out"
rm -f "$out/discourse.ready"
log() { echo "[ibx-discourse-bootstrap $(date -u +%FT%TZ)] $*" >&2; }

existing=""
if [ -s "$out/discourse.json" ]; then
  existing="$(ruby -rjson -e 'print JSON.parse(File.read(ARGV[0]))["api_key"].to_s' "$out/discourse.json" 2>/dev/null || true)"
fi

cd /var/www/discourse
t0=$(date +%s)
line="$(IBX_DISCOURSE_EXISTING_KEY="$existing" HOME=/home/discourse \
  chpst -u discourse:www-data -U discourse:www-data bundle exec rails runner /ibx/bootstrap/discourse_bootstrap.rb \
  | grep '^IBX_DISCOURSE_JSON ' | tail -n 1 || true)"
if [ -z "$line" ]; then
  log "rails runner produced no result"
  exit 1
fi
umask 077
printf '%s\n' "${line#IBX_DISCOURSE_JSON }" > "$out/discourse.json.tmp"
if [ -n "${BOOTSTRAP_OUT_UID:-}" ]; then
  chown "${BOOTSTRAP_OUT_UID}:${BOOTSTRAP_OUT_GID:-$BOOTSTRAP_OUT_UID}" "$out/discourse.json.tmp"
else
  chmod 0644 "$out/discourse.json.tmp"
fi
mv "$out/discourse.json.tmp" "$out/discourse.json"

# Verify the key against the live service (header values come from a file, not argv).
hdr="$(mktemp)"
ruby -rjson -e 'd=JSON.parse(File.read(ARGV[0])); puts "Api-Key: #{d["api_key"]}"; puts "Api-Username: #{d["api_username"]}"' \
  "$out/discourse.json" > "$hdr"
code="$(curl -s -o /dev/null -w '%{http_code}' -H @"$hdr" "$base/admin/users/list/active.json")"
rm -f "$hdr"
if [ "$code" != "200" ]; then
  log "API key verification failed: HTTP $code"
  exit 1
fi
reused="$(ruby -rjson -e 'print JSON.parse(File.read(ARGV[0]))["key_reused"]' "$out/discourse.json")"
log "ready in $(( $(date +%s) - t0 ))s (admin key verified, reused=$reused)"
touch "$out/discourse.ready"
if [ "${1:-}" = "--once" ]; then exit 0; fi
exec sleep infinity
