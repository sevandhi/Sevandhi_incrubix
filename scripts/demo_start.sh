#!/bin/sh
# Container command of Dockerfile.demo: start the all-in-one live demo on $PORT.
#   PUBLIC_BASE_URL defaults to Render's RENDER_EXTERNAL_URL (signed download links and the MRSS feed start there),
#   GIT_SHA to RENDER_GIT_COMMIT. --loop asyncio is required: the in-process "mock-platforms" host alias is a
#   socket.getaddrinfo mapping, which uvloop bypasses. One uvicorn worker: the job worker runs in the same process.
set -eu
PORT="${PORT:-10000}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-${RENDER_EXTERNAL_URL:-http://127.0.0.1:${PORT}}}"
GIT_SHA="${GIT_SHA:-${RENDER_GIT_COMMIT:-unknown}}"
export PORT PUBLIC_BASE_URL GIT_SHA
exec uvicorn --factory app.demo.allinone:create_demo_app --host 0.0.0.0 --port "$PORT" \
    --loop asyncio --workers 1 --no-access-log --timeout-graceful-shutdown 20
