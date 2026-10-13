#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${SIGNAL_ATLAS_PORT:-8000}"
fi
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "${SIGNAL_ATLAS_PORT:-8000}"
