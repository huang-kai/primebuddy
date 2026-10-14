#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${ASSISTANT_PYTHON:-python3}" "$ROOT/main.py" "$@"
