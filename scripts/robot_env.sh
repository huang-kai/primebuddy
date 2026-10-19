#!/usr/bin/env bash
# 仅加载当前型号的原厂环境，不修改配置或启停进程。
_primebuddy_load_robot_env() {
  local primebuddy_series primebuddy_setup
  local -a primebuddy_setups
  [ -r /robot/data/info/series ] || return 0
  IFS= read -r primebuddy_series < /robot/data/info/series || true
  case "$primebuddy_series" in
    Q1) primebuddy_setups=(/robot/software/housekeeper/bin/setup.bash /robot/software/app_proxy/bin/setup.bash) ;;
    T1) primebuddy_setups=(/robot/software/app_proxy/bin/setup.bash /robot/software/housekeeper/bin/setup.bash) ;;
    *) return 0 ;;
  esac
  if [ -r /robot/etc/basic_env ]; then
    source /robot/etc/basic_env >/dev/null 2>&1 || return 1
  fi
  for primebuddy_setup in "${primebuddy_setups[@]}"; do
    if [ -r "$primebuddy_setup" ]; then
      source "$primebuddy_setup" >/dev/null 2>&1
      return $?
    fi
  done
  printf '%s\n' '找不到当前机器人可用的原厂 ROS 环境。' >&2
  return 1
}
_primebuddy_load_robot_env
