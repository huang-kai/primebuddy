#!/usr/bin/env bash
# T1/Q1 共用机载标准接口；不加载应用私有的消息包或修改机器人配置。
_primebot_standard_sdk() {
  local series prefix python_dir setup variable item clean
  [ -r /robot/data/info/series ] || return 0
  IFS= read -r series < /robot/data/info/series || true
  case "$series" in T1|Q1) ;; *) echo 'Unsupported robot series' >&2; return 1 ;; esac
  prefix=/robot/software/interface/aimdk_msgs
  python_dir="$prefix/local/lib/python$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')/dist-packages"
  setup="$prefix/share/aimdk_msgs/local_setup.bash"
  if [ ! -d "$python_dir/aimdk_msgs" ] || [ ! -r "$setup" ]; then
    echo '机载标准 aimdk_msgs 不完整；未回退到应用私有消息包。' >&2
    return 1
  fi
  for item in rosidl_generator_c rosidl_generator_py rosidl_typesupport_c; do
    [ -r "$prefix/lib/libaimdk_msgs__$item.so" ] || {
      echo '机载标准 aimdk_msgs 类型库不完整。' >&2; return 1;
    }
  done
  [ -r /opt/ros/humble/setup.bash ] || { echo 'ROS 2 Humble environment missing' >&2; return 1; }
  source /opt/ros/humble/setup.bash >/dev/null 2>&1 || return $?
  if [ -r /robot/etc/basic_env ]; then
    source /robot/etc/basic_env >/dev/null 2>&1 || return $?
  fi
  # 清除调用者或 basic_env 留下的其他 aimdk_msgs 副本；保留 ROS 和业务依赖。
  for variable in PYTHONPATH LD_LIBRARY_PATH AMENT_PREFIX_PATH CMAKE_PREFIX_PATH; do
    clean=
    local -a entries
    IFS=: read -r -a entries <<< "${!variable-}"
    for item in "${entries[@]}"; do
      [ -n "$item" ] || continue
      case "$item" in "$prefix"|"$prefix"/*) continue ;; esac
      if [ -d "$item/aimdk_msgs" ] || [ -d "$item/share/aimdk_msgs" ] || [ -f "$item/libaimdk_msgs__rosidl_typesupport_c.so" ]; then
        continue
      fi
      clean="${clean:+$clean:}$item"
    done
    export "$variable=$clean"
  done
  source "$setup" >/dev/null 2>&1 || return $?
  export PYTHONPATH="$python_dir${PYTHONPATH:+:$PYTHONPATH}"
  export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export AMENT_PREFIX_PATH="$prefix${AMENT_PREFIX_PATH:+:$AMENT_PREFIX_PATH}"
  export PRIMEBOT_STANDARD_SDK="$prefix"
  python3 -c 'import aimdk_msgs, os; from pathlib import Path; assert Path(aimdk_msgs.__file__).resolve().is_relative_to(Path(os.environ["PRIMEBOT_STANDARD_SDK"]).resolve()), "aimdk_msgs loaded from a different prefix"' || return $?
}
# 原厂 ament 脚本不支持 nounset；只在加载过程中暂时关闭。
_primebot_had_nounset=false
case $- in *u*) _primebot_had_nounset=true; set +u ;; esac
if _primebot_standard_sdk; then _primebot_sdk_status=0; else _primebot_sdk_status=$?; fi
if $_primebot_had_nounset; then set -u; fi
unset -f _primebot_standard_sdk
return "$_primebot_sdk_status" 2>/dev/null || exit "$_primebot_sdk_status"
