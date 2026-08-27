#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/"
  exit 1
fi

NATIVE_PORT="${INDEXTTS_NATIVE_WEBUI_PORT:-7861}"
if ! [[ "$NATIVE_PORT" =~ ^[0-9]+$ ]] || ((NATIVE_PORT < 1 || NATIVE_PORT > 65535)); then
  echo "The native WebUI port must be a number from 1 to 65535."
  exit 1
fi

NATIVE_URL="http://127.0.0.1:${NATIVE_PORT}"
if curl -fsS "${NATIVE_URL}/" 2>/dev/null | grep -qiE 'IndexTTS|gradio'; then
  echo "The native IndexTTS WebUI is already running at ${NATIVE_URL}"
  if [[ "${INDEXTTS_NO_BROWSER:-0}" != "1" ]]; then
    open "$NATIVE_URL" 2>/dev/null || true
  fi
  exit 0
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${NATIVE_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${NATIVE_PORT} is already in use. Choose another native WebUI port."
  exit 1
fi

APP_ROOT="$(pwd -P)"
GRADIO_CACHE_DIR="${APP_ROOT}/outputs/gradio-cache"
export GRADIO_TEMP_DIR="$GRADIO_CACHE_DIR"

clean_gradio_cache() {
  local expected="${APP_ROOT}/outputs/gradio-cache"
  if [[ "$GRADIO_CACHE_DIR" != "$expected" ]]; then
    echo "Refusing to clean an unexpected Gradio cache path."
    return 1
  fi
  if [[ -L "$GRADIO_CACHE_DIR" || ( -e "$GRADIO_CACHE_DIR" && ! -d "$GRADIO_CACHE_DIR" ) ]]; then
    unlink "$GRADIO_CACHE_DIR"
  fi
  mkdir -p "$GRADIO_CACHE_DIR"
  find -P "$GRADIO_CACHE_DIR" -mindepth 1 -depth -delete
}

clean_gradio_cache
echo "Starting the optional upstream Gradio WebUI at ${NATIVE_URL}"
echo "Studio remains the default interface. Running both model processes can use more memory."
uv run --extra webui --locked python webui.py --host 127.0.0.1 --port "$NATIVE_PORT" &
server_pid=$!

stop_server() {
  if kill -0 "$server_pid" >/dev/null 2>&1; then
    kill -TERM "$server_pid" >/dev/null 2>&1 || true
    for _ in {1..32}; do
      if ! kill -0 "$server_pid" >/dev/null 2>&1; then
        wait "$server_pid" 2>/dev/null || true
        break
      fi
      sleep 0.25
    done
    if kill -0 "$server_pid" >/dev/null 2>&1; then
      kill -KILL "$server_pid" >/dev/null 2>&1 || true
      wait "$server_pid" 2>/dev/null || true
    fi
  fi
  clean_gradio_cache
}
trap stop_server EXIT INT TERM

for _ in {1..1200}; do
  if curl -fsS "${NATIVE_URL}/" >/dev/null 2>&1; then
    echo "Native WebUI is ready at ${NATIVE_URL}"
    if [[ "${INDEXTTS_NO_BROWSER:-0}" != "1" ]]; then
      open "$NATIVE_URL" 2>/dev/null || true
    fi
    wait "$server_pid"
    exit $?
  fi
  if ! kill -0 "$server_pid" >/dev/null 2>&1; then
    wait "$server_pid" || exit $?
    exit 1
  fi
  sleep 0.5
done

echo "Timed out while waiting for the native WebUI."
exit 1
