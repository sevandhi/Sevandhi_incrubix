#!/usr/bin/env bash
# Render deploy/lemmy/lemmy.hjson (template) with dev-only values from env, then start Lemmy.
# Values are never echoed.
set -euo pipefail

: "${LEMMY_HOSTNAME:=lemmy:8536}"
: "${LEMMY_ADMIN_USERNAME:=ibx_admin}"
: "${LEMMY_ADMIN_PASSWORD:?LEMMY_ADMIN_PASSWORD must be set}"
: "${LEMMY_ADMIN_EMAIL:=admin@lemmy.ibx.invalid}"
: "${PICTRS_API_KEY:=ibx-local-pictrs-key}"

template=/ibx/lemmy.hjson.tmpl
out=/tmp/lemmy-config.hjson

for name in LEMMY_HOSTNAME LEMMY_ADMIN_USERNAME LEMMY_ADMIN_PASSWORD LEMMY_ADMIN_EMAIL PICTRS_API_KEY; do
  value="${!name}"
  case "$value" in
    *'"'* | *'\'* | *$'\n'*) echo "entrypoint: $name contains a quote, backslash or newline; refusing" >&2; exit 2 ;;
  esac
done
if [ "${#LEMMY_ADMIN_PASSWORD}" -lt 10 ] || [ "${#LEMMY_ADMIN_PASSWORD}" -gt 60 ]; then
  echo "entrypoint: LEMMY_ADMIN_PASSWORD must be 10-60 characters (Lemmy rule)" >&2
  exit 2
fi

cfg="$(cat "$template")"
cfg="${cfg//@@LEMMY_HOSTNAME@@/"$LEMMY_HOSTNAME"}"
cfg="${cfg//@@LEMMY_ADMIN_USERNAME@@/"$LEMMY_ADMIN_USERNAME"}"
cfg="${cfg//@@LEMMY_ADMIN_PASSWORD@@/"$LEMMY_ADMIN_PASSWORD"}"
cfg="${cfg//@@LEMMY_ADMIN_EMAIL@@/"$LEMMY_ADMIN_EMAIL"}"
cfg="${cfg//@@PICTRS_API_KEY@@/"$PICTRS_API_KEY"}"
umask 077
printf '%s\n' "$cfg" > "$out"

export LEMMY_CONFIG_LOCATION="$out"
exec lemmy_server
