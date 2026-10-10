#!/usr/bin/env bash
# 用 source ./install.sh 安装，让当前终端立即获得 primebuddy 命令。
_primebuddy_install() {
  local root script result=0
  if [ -n "${BASH_VERSION:-}" ]; then
    script="${BASH_SOURCE[0]}"
  else
    script="${(%):-%x}"
  fi
  root="$(cd "$(dirname "$script")" && pwd)" || return
  "${ASSISTANT_PYTHON:-python3}" "$root/install.py" "$@" || result=$?
  if [ -f "$root/env.sh" ]; then
    . "$root/env.sh"
    hash -r
  fi
  unset -f _primebuddy_install
  return "$result"
}
_primebuddy_install "$@"
