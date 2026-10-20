#!/usr/bin/env bash
# Write fresh secrets into .env for a demo or prod run (demo/prod refuse the dev placeholders).
#   scripts/gen_secrets.sh            # APP_ENV=demo
#   scripts/gen_secrets.sh prod
# Keys already in .env are kept, so running it twice changes nothing. .env is git-ignored; never commit it.
# With APP_ENV=demo|prod the api and worker refuse to start on the dev placeholders from compose.yaml.
# Run it before the first `docker compose up` (Postgres keeps the password of an existing pg_data volume).
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
touch .env
add() { grep -q "^$1=" .env || printf '%s=%s\n' "$1" "$2" >> .env; }
token() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
add APP_ENV "${1:-demo}"
add API_TOKEN "$(token)"
add POSTGRES_PASSWORD "$(token)"
add S3_ACCESS_KEY "ibx$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
add S3_SECRET_KEY "$(token)"
add SECRETS_KEY "$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
add CALLBACK_SECRET "$(token)"
echo "secrets are in .env (values not printed). The API token is the API_TOKEN line."
