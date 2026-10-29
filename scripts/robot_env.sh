#!/usr/bin/env bash
# 仅加载当前型号的原厂环境，不修改配置或启停进程。
_primebuddy_load_robot_env() {
  local primebuddy_series primebuddy_setup primebuddy_interface primebuddy_python
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
      source "$primebuddy_setup" >/dev/null 2>&1 || return $?
      # Q1 的 housekeeper 私有消息包可能早于当前运控；Python 与 C 类型库必须成套加载。
      primebuddy_interface=/robot/software/interface/aimdk_msgs
      if [ "$primebuddy_series" = Q1 ] && [ -d "$primebuddy_interface" ]; then
        primebuddy_python="$primebuddy_interface/local/lib/python3.10/dist-packages"
        if [ ! -d "$primebuddy_python/aimdk_msgs" ] || [ ! -r "$primebuddy_interface/lib/libaimdk_msgs__rosidl_typesupport_c.so" ]; then
          printf '%s\n' 'Q1 当前接口包不完整，拒绝混用旧 Python 消息与新类型库。' >&2
          return 1
        fi
        export PYTHONPATH="$primebuddy_python${PYTHONPATH:+:$PYTHONPATH}"
        export LD_LIBRARY_PATH="$primebuddy_interface/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        export AMENT_PREFIX_PATH="$primebuddy_interface${AMENT_PREFIX_PATH:+:$AMENT_PREFIX_PATH}"
      fi
      return 0
    fi
  done
  printf '%s\n' '找不到当前机器人可用的原厂 ROS 环境。' >&2
  return 1
}
_primebuddy_load_robot_env
