#!/usr/bin/env bash
# Local free LLM helper. Ollama (MIT) + an Apache-2.0 model; no API key, prompts stay on this machine.
#
#   scripts/llm_ollama.sh volume              create the shared model volume ibx_ollama_models (idempotent)
#   scripts/llm_ollama.sh pull [model]        start the compose "llm" profile and pull the model into the volume
#   scripts/llm_ollama.sh serve-local [port]  a standalone Ollama on 127.0.0.1:<port> (default 11439) for host-side
#                                             tools such as scripts/eval_rules_capture.py (loopback only)
#   scripts/llm_ollama.sh stop-local          stop that standalone container
#
# Default model: qwen2.5:1.5b-instruct (Apache-2.0). qwen2.5:7b-instruct is Apache-2.0 too (slower on CPU).
# Do NOT use qwen2.5:3b-instruct: its licence is the Qwen Research licence (non-commercial only).
# Uses COMPOSE_PROJECT_NAME from the environment for the compose commands.
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL_DEFAULT="${OLLAMA_MODEL:-qwen2.5:1.5b-instruct}"
VOLUME="ibx_ollama_models"
IMAGE="ollama/ollama:0.35.1"
LOCAL_NAME="ibx_ollama_local"

ensure_volume() {
  docker volume inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME" >/dev/null
  echo "volume $VOLUME ready"
}

case "${1:-pull}" in
  volume)
    ensure_volume
    ;;
  pull)
    model="${2:-$MODEL_DEFAULT}"
    case "$model" in
      qwen2.5:3b*|qwen2.5:72b*) echo "refused: $model is not Apache-2.0 (Qwen research/Qwen licence)" >&2; exit 2 ;;
    esac
    ensure_volume
    docker compose --profile llm up -d --wait ollama
    docker compose --profile llm exec -T ollama ollama pull "$model"
    docker compose --profile llm exec -T ollama ollama list
    ;;
  serve-local)
    port="${2:-${IBX_OLLAMA_PORT:-11439}}"
    ensure_volume
    if ! docker ps --format '{{.Names}}' | grep -qx "$LOCAL_NAME"; then
      docker run -d --rm --name "$LOCAL_NAME" -p "127.0.0.1:${port}:11434" -v "$VOLUME:/root/.ollama" \
        -e OLLAMA_KEEP_ALIVE=30m "$IMAGE" >/dev/null
    fi
    for _ in $(seq 1 60); do
      if docker exec "$LOCAL_NAME" ollama list >/dev/null 2>&1; then break; fi
      sleep 1
    done
    docker exec "$LOCAL_NAME" ollama list
    echo "OLLAMA_URL=http://127.0.0.1:${port}/v1"
    ;;
  stop-local)
    docker stop "$LOCAL_NAME" >/dev/null 2>&1 || true
    echo "stopped $LOCAL_NAME"
    ;;
  *)
    sed -n '2,13p' "$0"; exit 2 ;;
esac
