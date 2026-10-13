#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python -m venv --system-site-packages .venv
fi
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
