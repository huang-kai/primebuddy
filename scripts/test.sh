#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/_deps${PYTHONPATH:+:$PYTHONPATH}"
"${ASSISTANT_PYTHON:-python3}" -m unittest discover -s "$ROOT/tests" -v

# 独立技能的测试留在各自项目；工作区总体验证时一起运行。
for skill in "$ROOT"/../primebuddy-skills/*; do
  if [[ -d "$skill/tests" ]]; then
    "${ASSISTANT_PYTHON:-python3}" -m unittest discover -s "$skill/tests" -v
  fi
done
