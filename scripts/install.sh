#!/usr/bin/env bash
# 用 source ./scripts/install.sh 安装，让当前终端立即获得 primebuddy 命令。
_primebuddy_install() {
  local root script target="${PRIMEBUDDY_HOME:-$HOME/.primebuddy}" result=0
  local -a arguments=("$@")
  if [ -n "${BASH_VERSION:-}" ]; then
    script="${BASH_SOURCE[0]}"
  else
    script="${(%):-%x}"
  fi
  root="$(cd "$(dirname "$script")/.." && pwd)" || return
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --home) shift; target="${1:-}" ;;
      --home=*) target="${1#--home=}" ;;
    esac
    [ "$#" -eq 0 ] || shift
  done
  case "$target" in '~/'*) target="$HOME/${target#\~/}" ;; esac
  "${ASSISTANT_PYTHON:-python3}" "$root/main.py" install "${arguments[@]}" || result=$?
  if [ -f "$target/bin/env.sh" ]; then
    . "$target/bin/env.sh"
    hash -r
  fi
  unset -f _primebuddy_install
  return "$result"
}
_primebuddy_install "$@"
