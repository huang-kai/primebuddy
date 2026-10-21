#!/usr/bin/env bash
# 用 source ./scripts/install.sh 安装，让当前终端立即获得 primebuddy 命令。
_primebuddy_install() {
  local root script environment response result=0
  local -a arguments=("$@")
  if [ -n "${BASH_VERSION:-}" ]; then
    script="${BASH_SOURCE[0]}"
  else
    script="${(%):-%x}"
  fi
  root="$(cd "$(dirname "$script")/.." && pwd)" || return
  response="$(mktemp)" || return
  "${ASSISTANT_PYTHON:-python3}" "$root/main.py" install "${arguments[@]}" --shell-env-file "$response" || result=$?
  # 远程安装不会返回本板入口，避免加载运控板上遗留的旧版本。
  if IFS= read -r environment < "$response" && [ -f "$environment" ]; then
    . "$environment"
    hash -r
  fi
  rm -f "$response"
  unset -f _primebuddy_install
  return "$result"
}
_primebuddy_install "$@"
