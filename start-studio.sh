#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/"
  exit 1
fi

# The open checkout intentionally omits large model weights. When a complete
# local checkout is beside it, reuse those weights without copying them into
# the open tree. An explicit environment variable always wins.
if [[ -z "${INDEXTTS_CHECKPOINTS_DIR:-}" ]]; then
  app_root="$(pwd -P)"
  for candidate in \
    "${app_root}/checkpoints" \
    "${app_root}/../IndexTTS/checkpoints" \
    "${HOME}/Applications/IndexTTS/checkpoints"; do
    if [[ -f "${candidate}/config.yaml" &&
          -f "${candidate}/gpt.pth" &&
          -f "${candidate}/s2mel.pth" &&
          -f "${candidate}/multilingual_zh_ja_yue_char_del.tiktoken" ]]; then
      INDEXTTS_CHECKPOINTS_DIR="$(cd "${candidate}" && pwd -P)"
      export INDEXTTS_CHECKPOINTS_DIR
      echo "Using local IndexTTS model checkpoints: ${INDEXTTS_CHECKPOINTS_DIR}"
      break
    fi
  done
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required to accept browser recordings and local media files."
  exit 1
fi

STUDIO_PORT="${INDEXTTS_STUDIO_PORT:-7860}"
if ! [[ "$STUDIO_PORT" =~ ^[0-9]+$ ]] || ((STUDIO_PORT < 1 || STUDIO_PORT > 65535)); then
  echo "The port must be a number from 1 to 65535."
  exit 1
fi

STUDIO_URL="http://127.0.0.1:${STUDIO_PORT}"
if curl -fsS "${STUDIO_URL}/api/status" 2>/dev/null | grep -q '"model":"IndexTTS-2.5"'; then
  echo "IndexTTS Studio is already running at ${STUDIO_URL}"
  exit 0
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${STUDIO_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${STUDIO_PORT} is already in use. Choose another port."
  exit 1
fi

echo "Starting IndexTTS Studio at http://127.0.0.1:${STUDIO_PORT}"
echo "The first launch can take a few minutes while audio dependencies warm up."
uv run --extra studio --locked python studio_server.py &
server_pid=$!

stop_server() {
  if kill -0 "$server_pid" >/dev/null 2>&1; then
    kill -TERM "$server_pid" >/dev/null 2>&1 || true
    for _ in {1..32}; do
      if ! kill -0 "$server_pid" >/dev/null 2>&1; then
        wait "$server_pid" 2>/dev/null || true
        return
      fi
      sleep 0.25
    done
    echo "The service did not stop within 8 seconds; ending this launch process."
    kill -KILL "$server_pid" >/dev/null 2>&1 || true
    wait "$server_pid" 2>/dev/null || true
  fi
}
trap stop_server EXIT INT TERM

wait "$server_pid"
