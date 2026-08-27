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
NATIVE_STARTUP_URL="${NATIVE_URL}/gradio_api/startup-events"

# Gradio performs its own localhost HTTP check during launch. Keep that check
# off any inherited proxy without changing proxy settings for other hosts.
loopback_no_proxy="${NO_PROXY:-${no_proxy:-}}"
for loopback_host in 127.0.0.1 localhost; do
  case ",${loopback_no_proxy}," in
    *",${loopback_host},"*) ;;
    *) loopback_no_proxy="${loopback_no_proxy:+${loopback_no_proxy},}${loopback_host}" ;;
  esac
done
export NO_PROXY="$loopback_no_proxy"
export no_proxy="$loopback_no_proxy"

native_is_ready() {
  curl -fsS --max-time 2 "${NATIVE_URL}/" >/dev/null 2>&1 &&
    curl -fsS --max-time 2 "$NATIVE_STARTUP_URL" >/dev/null 2>&1
}

if native_is_ready; then
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
NATIVE_STDERR_LOG="${APP_ROOT}/outputs/logs/native-webui-current.stderr.log"
NATIVE_ERROR_LOG="${APP_ROOT}/outputs/logs/native-webui-last-error.log"
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

preserve_child_stderr() {
  local status="$1"
  echo "Native WebUI exited unexpectedly with status ${status}." >&2
  if [[ -s "$NATIVE_STDERR_LOG" ]]; then
    mkdir -p "$(dirname "$NATIVE_ERROR_LOG")"
    cp -p "$NATIVE_STDERR_LOG" "$NATIVE_ERROR_LOG"
    echo "Original stderr was printed above and saved to: ${NATIVE_ERROR_LOG}" >&2
  else
    echo "The child process did not write any stderr." >&2
  fi
}

clean_gradio_cache
echo "Starting the optional upstream Gradio WebUI at ${NATIVE_URL}"
echo "Studio remains the default interface. Running both model processes can use more memory."
mkdir -p "$(dirname "$NATIVE_STDERR_LOG")"
: > "$NATIVE_STDERR_LOG"
tail -n 0 -f "$NATIVE_STDERR_LOG" >&2 &
stderr_tail_pid=$!
uv run --extra webui --locked python webui.py --host 127.0.0.1 --port "$NATIVE_PORT" \
  2>>"$NATIVE_STDERR_LOG" &
server_pid=$!
stop_requested=0

stop_stderr_stream() {
  if kill -0 "$stderr_tail_pid" >/dev/null 2>&1; then
    kill -TERM "$stderr_tail_pid" >/dev/null 2>&1 || true
    wait "$stderr_tail_pid" 2>/dev/null || true
  fi
}

stop_server() {
  stop_requested=1
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
  stop_stderr_stream
  if [[ -f "$NATIVE_STDERR_LOG" ]]; then
    /bin/unlink "$NATIVE_STDERR_LOG"
  fi
  clean_gradio_cache
}
trap stop_server EXIT INT TERM

for _ in {1..1200}; do
  if native_is_ready; then
    echo "Native WebUI is ready at ${NATIVE_URL}"
    if [[ -f "$NATIVE_ERROR_LOG" ]]; then
      /bin/unlink "$NATIVE_ERROR_LOG"
    fi
    if [[ "${INDEXTTS_NO_BROWSER:-0}" != "1" ]]; then
      open "$NATIVE_URL" 2>/dev/null || true
    fi
    server_status=0
    wait "$server_pid" || server_status=$?
    if ((server_status != 0 && stop_requested == 0)); then
      preserve_child_stderr "$server_status"
    fi
    exit "$server_status"
  fi
  if ! kill -0 "$server_pid" >/dev/null 2>&1; then
    server_status=0
    wait "$server_pid" || server_status=$?
    ((server_status == 0)) && server_status=1
    preserve_child_stderr "$server_status"
    exit "$server_status"
  fi
  sleep 0.5
done

root_status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "${NATIVE_URL}/" 2>/dev/null || true)
startup_status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "$NATIVE_STARTUP_URL" 2>/dev/null || true)
echo "Timed out while waiting for the native WebUI (root=${root_status:-000}, startup-events=${startup_status:-000})."
exit 1
