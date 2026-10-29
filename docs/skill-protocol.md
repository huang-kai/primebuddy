# 开发一个 skill

一个 skill 最少只需要一个 `SKILL.md`。它告诉框架：这个技能叫什么、什么时候使用，以及如何完成任务。需要执行业务操作时再加脚本，需要第三方库时再加依赖。

可以复制技能仓库的 [通用模板](https://github.com/huang-kai/primeskills/blob/main/templates/my-skill/SKILL.md)，并参考 [编写指南](https://github.com/huang-kai/primeskills/blob/main/templates/README.md)。目录采用常见的 `SKILL.md` 加可选 `scripts/`、`references/`、`assets/`；已有程序也可以保留自己的 `main.py`、`src/` 结构。

发现技能时，模型只看到名称、简短 `description` 和位置；选中后读取完整正文，再按需读参考资料。摘要简短是为了控制发现成本，正文仍需写清使用步骤、例子、结果校验和失败处理。不要求每个技能实现 `run(context)` 等宿主函数。

下面的例子在框架源码目录中操作，技能放在相邻的 `my-skills/hello/`。

### 1. 编写技能说明

创建目录：

```bash
mkdir -p ../my-skills/hello
```

在 `../my-skills/hello/SKILL.md` 中写入：

```markdown
---
name: hello
description: 用户要求向某个人问好时使用。
metadata:
  version: "0.1.0"
---

没有提供名字时，先询问名字。
用户提供名字后，用一句简短中文问候。
```

`name` 使用小写字母、数字和单连字符，最多 64 字符，并与目录名一致；`description` 最多 1024 字符，写清用途和触发条件。较复杂的正文可按“何时使用、准备、执行流程、校验与反馈、失败与取消、示例、参考”组织，按业务删除无关章节。只提供说明的技能，到这里就可以安装。

### 2. 按需添加业务代码

如果任务需要运行代码，可以增加 `scripts/greet.py`：

```text
hello/
├── SKILL.md
└── scripts/
    └── greet.py
```

```python
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('name')
args = parser.parse_args()
print('你好，' + args.name)
```

同时把 `SKILL.md` 正文改为：

```text
没有提供名字时，先询问名字。
运行 scripts/greet.py，将名字作为一个命令行参数。
根据脚本的实际输出回复用户。
```

脚本可以使用 Python、Bash 或 JavaScript，不必导入 PrimeBuddy。业务执行时通过参数接收输入，通过标准输出返回结果；不要在业务脚本中用 `input()` 等待交互。安装时的交互配置使用下方的配置向导。

普通文本输出即可，不强制某种 JSON 结构。长任务可以处理标准终止信号或提供自己的停止命令，不需要为 Buddy 增加 `cancel(context)`、`shutdown(context)`。设备操作的停止与清理仍由业务程序负责，进程结束本身不代表机器人已停止。

只用 Python 标准库时无需额外依赖。需要第三方库时，在技能根目录的 `requirements.txt` 中使用 `包名==版本号` 声明，安装器会将依赖放到技能自己的 `_deps/`。

### 平台技能：声明 CLI 协议与运行模式

上面的纯文档和通用脚本属于保留的兼容路径。新平台技能应在 `cfg/pkg.yaml` 声明 `primebuddy.version: 1` 和安装向导入口，再定义具名动作、参数及资源。下面的 `scripts/configure.py` 必须实际存在并实现向导（无须配置时也应正常返回），完整约定见[安装向导协议](installation-protocol.md)。

```yaml
primebuddy:
  version: 1
  mode: task
  cli:
    configure: [scripts/configure.py, --config, '{config}']
    actions:
      greet:
        command: [scripts/greet.py, '{name}']
        parameters:
          name: {type: string}
        resources: []
        timeout_seconds: 30
        description: 正在问候 {name}
```

`command` 是技能内脚本和参数列表，不是 shell 字符串。模板只替换已声明参数和 `{config}`、`{data}`、`{runtime}`；模型不能改脚本入口、资源或执行期限。未适配的通用脚本默认共用 `skill.scripts`，硬期限为 600 秒。已适配的技能只暴露声明动作，避免从原始脚本入口绕过资源和清理约束。

`mode` 未声明时默认为 `task`，普通任务保持现有执行流程；`exclusive` 表示由技能负责整局语音交互。模式属于技能包元数据，不由 LLM 或游戏中的语音指令修改。`primebuddy.cli` 不能与旧版 `entrypoint/actions/setup/configure` 声明混用。

宿主仍可在 `~/.primebuddy/cfg/skill-adapters.yaml` 以技能名映射同样的 CLI 内容，优先级为用户配置优先；协议 v1 使用技能包声明，旧技能再回退宿主默认映射。点歌台、易来及旧名成语接龙的兼容映射见 [skill-adapters.example.yaml](../cfg/skill-adapters.example.yaml)。用户配置可替换命令，但不会取消技能声明的独占模式；旧包的 `null` 可禁用普通适配；v1 覆盖不得移除 configure 入口，独占技能遇到禁用或缺少启动、状态、恢复命令时明确报错，不回退到通用脚本。`configure` 映射交互式配置命令；`context`、动作的 `preflight`、`confirmation` 可映射已有的只读查询或预览命令。它们不要求技能实现同名 Python 函数。配置与登录只在 Console 进行。

设备程序应自行处理标准终止信号和原厂业务收尾。对需要确认外部设备释放的技能，可以配置 `status` 和 `recover` 命令：前者返回实际状态，后者执行技能自身提供的恢复操作。设备隔离需要状态查询返回 `status: success`、`resources_released: true` 才能解除。进程清理未确认时还须独立核对旧进程的 PID 和启动身份，设备空闲不能代替进程退出证明；查询失败、超时或无法确认均保留私有记录，阻止占用相关资源。普通文本 CLI 不需要使用这组可选 JSON 字段。

独立多轮语音应用在技能包声明 `primebuddy.mode: exclusive`，并在 `primebuddy.cli` 中提供启动动作、`status` 和 `recover`。Buddy 自动为动作声明独占交互和麦克风、扬声器资源；动作必须是本批唯一任务。应用以前台进程覆盖整局，Buddy 在此期间暂停自己的语音识别和任务分发，原厂唤醒及收音前端继续供游戏使用。应用退出且独立状态查询确认资源释放后，Buddy 清除旧音频事件、开启新的监听轮次，等待用户新请求，不根据原启动请求自动继续安排其他技能。成语接龙的[完整包配置](https://github.com/huang-kai/primeskills/blob/main/chengyu-chain/cfg/pkg.yaml)使用 `play --hosted`，原厂语音模式仍由 Buddy 管理；应用不得重复接管或恢复模式。正常退出由游戏意图和静默时限控制，宿主另设 86400 秒硬上限。旧宿主映射中的动作级 `interaction: exclusive` 继续兼容。

Buddy 与独立成语应用共用本机 `/tmp/primebot-voice-frontend.lock`，前端清理完成前保持持锁；hosted 游戏由 Buddy 持有该锁，不重复获取。该互斥只覆盖同一控制器、同一文件系统，双板设备不能分别在两块板上启动语音应用。原有私有运行目录的 `audio.lock` 继续生效；共享锁拒绝符号链接和不安全的文件权限，不要删除正在使用的锁文件。

独占期间宿主处于 `exclusive` 语音暂停状态：音频回调直接忽略，不做 VAD 分句、ASR、LLM 或新任务分发；只保留原厂最新状态供交还后使用。游戏自行订阅原厂处理后音频并管理收音。原厂 IDLE 和唤醒不切回 Buddy。正常结束或异常退出后，先核对进程和设备资源，再清除旧轮事件并恢复宿主；不会把游戏期间的话当成新指令。资源清理暂未确认时，Buddy 保持存活和语音暂停，每次检查失败后等待 5 秒，再调用该技能的恢复与状态命令；只有确认释放才恢复监听，可通过停止 Buddy 退出等待。

旧包的 `entrypoint/actions` 和可选 hooks 继续通过一次性 worker 子进程兼容，业务模块不再导入 Buddy 主进程；它们仍属于旧宿主协议，且同样只暴露已声明动作。新的点歌台和易来已迁移普通 CLI，不采用这条路径。

### 进程期限与取消

每次执行有独立进程组、任务和所属轮次 ID，以及私有输出文件和执行记录。硬期限届满或本轮取消时，监督器依次发送 SIGINT、SIGTERM、SIGKILL，默认宽限分别为 3、2、2 秒，并核对进程清理结果。点歌适配将首段宽限设为 25 秒，容纳订阅匹配、原厂停止请求与状态确认；可通过宿主动作的 `stop_grace_seconds` 配置三段有界宽限。技能无需实现 Buddy 的取消函数。当前会追踪已观察到的后代，包括另建进程组的子进程；首次采样前瞬间脱离父子关系的进程仍有边界，当前没有 cgroup/subreaper 隔离或重启接管保证。

进程退出与机器人业务结束分别确认。点歌正常完成等待原厂 `PlayMediaFile` 的播放生命周期；取消或异常后检查技能自身恢复记录，必要时调用它的恢复命令。独占应用清理未确认时，按上述规则保持宿主存活并重试，不播报、不启动新任务；普通 task 的清理未确认仍会退出语音会话。启动首次播报前也会检查遗留状态。执行记录用于审计，不会自动重放上一次命令。

### 3. 验证并安装

添加脚本后，先直接运行，确认业务结果：

```bash
python3 ../my-skills/hello/scripts/greet.py 张三
```

再安装到框架，检查是否被发现：

```bash
primebuddy skill install ../my-skills/hello
primebuddy skill list
primebuddy skill catalog
```

已经配置模型时，还可以用文字模拟一次请求：

```bash
primebuddy simulate --skills-dir ../my-skills --text "向张三问好"
```

文字模拟会调用 LLM，用来检查模型是否选中技能、读取说明并生成任务；不会实际执行业务脚本或控制设备。真实设备能力仍需要在相应环境中验证。

在技能的 `tests/` 中保留主要功能、错误输入和业务失败的测试。涉及长任务时，也要检查取消和清理逻辑。

### 4. 打包和部署

```bash
python3 src/primebuddy/tools/build_release.py --skill ../my-skills/hello
```

生成的包是 `dist/hello-0.1.0.rpk`。把它复制到已安装 PrimeBuddy 的机器上，再使用 `primebuddy skill install` 安装。更新已有技能时增加 `--replace`。

[返回文档导航](README.md) · [返回项目首页](../README.md)
