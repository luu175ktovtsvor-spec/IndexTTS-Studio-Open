#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PYTHONDONTWRITEBYTECODE=1

uv run --extra studio --extra test --locked python -m pytest \
  -p no:cacheprovider \
  -q \
  tests/test_studio_contract.py \
  tests/test_webui_syntax.py
