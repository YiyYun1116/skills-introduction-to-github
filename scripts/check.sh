#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m unittest discover -s tests -v
fi
exec python -m unittest discover -s tests -v
