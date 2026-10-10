# T1 系统环境、SDK 与旧技能迁移

## 部署约定

框架和技能都在 T1 上使用系统 Python 3 与已有 ROS 2 Humble。安装器固定使用安装时的解释器，不创建 venv，不安装 ROS，不编译或复制消息包，不修改 DDS 配置。框架本身不依赖 pip；技能需要第三方 Python 包时，通过系统 `python3 -m pip --target <技能目录>/_deps` 安装。

SDK 目录可以作为独立参考保留在工作区。手册《PrimeBot_SDK快速入门与集成指南》3.1.2 将源码放在 `deps/` 并执行 `colcon build`；3 章此前提包含 SDK 编译。Python 脚本不用编译，不代表原始 `.msg/.srv` 目录已经是可导入模块。

运行依赖是已编译的 `aimdk_msgs` Python 绑定和 ROS 类型支持动态库。T1 原厂已经提供这些文件时直接使用，框架及技能都不用各编一份。若固件没有所需类型，先取得匹配固件的消息包；本安装器会报缺失，不自动从旧 SDK 编译覆盖。

本地 SDK 曾与部署类型存在差异，例如 `AudioFile.media_role`、`JointState` 温度字段类型。当前音频链路还用到本地 SDK 未包含的 `ProcessedAudioOutput` 等部署接口。因此，不能用“复制 aimdk_msgs 源码到项目”替代加载机器人现有消息包。

## 加载已有 ROS 环境

加载 shell 环境只是让系统 Python 找到已有模块和动态库，不会创建虚拟环境。沿用当前机器对应板已验证的入口；不要把两块板的路径混用，也不要从外部 SDK setup 覆盖原厂 DDS 环境。

当前语音会话运行于 SoC 1，历史部署使用：

```bash
source /robot/etc/basic_env
source /robot/software/housekeeper/bin/setup.bash
python3 -c 'import rclpy; from aimdk_msgs.msg import AsrResult; from aimdk_msgs.srv import PlayTts; print("SDK voice imports OK")'
```

SoC 0 的历史只读验证使用 `/robot/software/app_proxy/bin/setup.bash`。这些是已记录路径，升级固件后需重新确认；安装命令继承当前 shell 的环境，不自动遍历或覆盖厂商配置。导入成功只证明类型可用，不证明服务端存在或播报出声。

之后在解压后的 `primebuddy/` 目录执行 `source ./install.sh`，应用就地运行，当前终端和新终端都能使用 `primebuddy` 命令。若直接执行 `python3 install.py`，结束后运行一次 `source ./env.sh`。框架和所有已安装 skill 使用同一系统解释器。旧版本残留 venv 不会再被新入口使用，也不会被安装器擅自删除；其中的依赖不会迁移进系统 Python。

## ASR/TTS 接口

使用 `primebuddy start` 启动。框架默认发布 `/primebuddy/asr_result`，服务入口为 `/primebuddy/PlayTts`。`primebuddy start --sdk-bridge native` 改为 SDK 原名 `/aima/agent/asr_result`、`/aimdk_5Fmsgs/srv/PlayTts`；`--sdk-bridge off` 关闭两者。接口随真实语音会话启动和释放，安装阶段不创建 ROS 节点。

ASR 使用真实 `aimdk_msgs.msg.AsrResult`，每次非空识别产生一次最终结果，填写发布时间、递增序号和唯一 event_id。上游未给出置信度、语言检测或精确采集时间，因此 confidence=0、language 为空、meas_stamp 为默认值；下游应将这些字段视为未提供。发布 QoS 为 RELIABLE/VOLATILE，支持 SDK sensor-data 订阅和普通可靠订阅，不向新订阅者重放旧指令。

TTS 使用真实 `aimdk_msgs.srv.PlayTts`。保留请求/响应的嵌套结构，正确填写 `response.header.header.code`、`response.header.status.value` 和 `response.tts_resp`，回传 text、domain、trace_id 与优先级。SDK 示例读取的 `tts_resp.is_success` 可以继续使用，但只能表示接单结果。

首版的服务范围：

- 文本1–100字，非空 domain，`priority_level.value=6`、`priority_weight=0`。
- 只在框架空闲、没有未处理语音或正在采集的语音片段时接单；最多保留一条待播文本。收音、计划、技能执行、播音期间返回失败。
- 空闲时 `is_interrupted` 为 true/false 都可接单；忙时都拒绝。没有实现原厂的跨优先级抢占、同级排队，也不会接受高优先级后当普通交互播放。
- 回调只登记请求，主循环使用千问 TTS、已有音频互斥锁与完成检测播放；播放时暂停收音，成功后恢复监听或原等待唤醒状态。失败释放待播项并等待唤醒，不自动重试。
- `estimated_duration=0`。服务成功返回后合成或播放仍可能失败；没有可供旧客户端等待的原厂完成话题。日志会记录播放异常类型。需要依据完成结果串行动作的旧技能，应迁入框架任务依赖流程，不能将 accepted 当作 finished。

服务回调随现有 ROS executor 运行，云端同步请求期间可能延迟处理。这是基本 SDK 格式兼容，不是原厂 TTS 调度器的完整替代，也不用于紧急告警。

## only_voice 与入口占用

用户已实测 only_voice 下原厂 TTS 不出声。2026-10-06 的项目记录同时显示原厂 `PlayTts` 返回 accepted=true；normal 对照可以出声。因而接管合成/播放与接管服务名称是两个独立步骤。

框架在启动前留出 DDS 发现窗口，检查 ASR 发布者和 TTS 服务端，运行中继续检测；出现同名外部入口就拒绝继续。发现过程异步，无法保证另一个进程不会在检查之后创建同名服务，因此默认保留独立命名空间。不得依靠两台同名服务随机分配请求实现切换。退出先撤销框架接口，再让已有恢复管理恢复原厂模式。

旧 ROS 客户端可以只改启动参数，不改消息定义（下例是客户端启动形式，替换为实际旧脚本）：

```bash
python3 old_skill.py --ros-args \
  -r /aima/agent/asr_result:=/primebuddy/asr_result \
  -r /aimdk_5Fmsgs/srv/PlayTts:=/primebuddy/PlayTts
```

前提是旧脚本把参数交给 `rclpy.init()`；如果代码固定使用 `args=[]`，需在其配置中改入口。重映射用法依据 [ROS 2 Humble 官方说明](https://docs.ros.org/en/humble/How-To-Guides/Migrating-from-ROS1/Migrating-Scripts.html)。框架本身使用 `--sdk-bridge` 选择入口。

## skill 接入原生 topic/service

新 skill 保留安装清单与业务入口，ROS 代码放在自己的 `src/` 中，直接使用系统 `rclpy` 和部署消息类型。无需框架为每个 topic 再定义一层消息类，也不需要改主框架。

迁移分两种情况：

| 旧逻辑 | 接入方式 |
| --- | --- |
| 查询机器人状态、订阅电池/触摸、调用已验证业务服务 | 保留原生 ROS 类型、topic/service 名称和实测 QoS，业务代码放进 skill |
| 长驻订阅 ASR、识别意图、播报执行结果 | 意图与参数放入 manifest/SKILL.md；由框架调度 run 并统一反馈 |
| 原 ASR 消费者暂时作为独立程序保留 | 可重映射到兼容话题；不要再与框架同时处理同一指令，防止重复执行 |
| 直接调用原厂 PlayTts 并等待完成 | 独立客户端先检查接单结果；需要完成依赖时迁为框架任务，不能继续依赖未经验证的状态话题 |
| 操作舵机、动作、运动模式或原厂进程 | 单独核实部署定义、前置条件和授权，不因接口迁移而自动启用 |

skill 的 `run()` 在工作线程执行。使用独立的 `rclpy.context.Context` 和 `SingleThreadedExecutor` 管理自己的节点；不要对全局 Context 调用 `rclpy.shutdown()`，也不要从多个线程 spin 同一个 executor。循环中执行 `context.checkpoint()`，设置截止时间，finally 释放订阅、节点、executor 和自己的 ROS Context。不要把框架播音节点交给 skill 操作。

将下面的导入放在 `run()` 的真实执行分支，模拟分支直接返回模拟结果，开发机不要求安装 ROS：

```python
import rclpy
from aimdk_msgs.msg import Bms
from rclpy.context import Context as RosContext
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
```

例如电量技能在自己的节点订阅 `/aima/hal/bms/state`，使用 sensor-data QoS，只读取 `remaining_capacity_percentage`，不输出序列号等身份信息。按上述生命周期管理节点即可，无需给主框架增加电池接口或打包另一套 SDK。

不要把 `rclpy/aimdk_msgs` 写入 manifest 的 pip dependencies，也不要从旧项目复制虚拟环境。旧滴滴客户端存在 `priority_weight=100` 的写法，超出 SDK 请求定义的0–99范围；接入本版兼容 TTS 时改为0。旧项目没有因此被自动修改、启动或宣称已全部迁移。

## 验证边界

本地测试直接读取随附 SDK 的 `.msg/.srv` 定义构造夹具，覆盖嵌套字段、接单/拒绝、晚到冲突、ASR 序号、播音静音与恢复、部分启动失败清理，并保留既有对话回归测试。这验证代码契约，不能代替 T1 生成类型、DDS 发现与真实听感验证。

真机联调应先只读确认所在板的 Python/ROS 类型和 only_voice 入口占用情况，再在获得设备操作授权后验证一次识别发布、一次外部短句播报、忙时拒绝和退出恢复。本轮未连接或更改机器人。
