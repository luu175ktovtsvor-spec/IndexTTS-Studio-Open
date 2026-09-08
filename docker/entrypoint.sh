#!/usr/bin/env bash
set -euo pipefail

if [[ "${INDEXTTS_AUTO_DOWNLOAD:-1}" == "1" ]]; then
  echo "Checking IndexTTS 2.5 model files..."
  python docker/prepare_model.py "${INDEXTTS_CHECKPOINTS_DIR:-/app/checkpoints}"
else
  echo "Automatic model download is disabled."
fi

exec "$@"
