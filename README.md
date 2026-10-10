# PrimeBuddy

PrimeBuddy 是一个通过独立 skill 扩展能力的机器人应用平台。框架将用户的技能指令转为计划，调度已安装、已配置并启用的 skill，再反馈执行结果；skill 负责自己的安装配置和业务执行。

当前版本 **0.3.0**，已实现的设备入口是 **启元 T1 上面向 skill 的语音指令入口**，模型服务使用千问。核心使用 Python 3.10+ 标准库；在 T1 上直接复用系统 Python 和已有 ROS 2，不需要 Node.js、虚拟环境或重新编译 SDK。当前还没有 Web 界面、后台服务管理或其他机器人适配。

## 能力边界

**PrimeBuddy 的定位是运行已安装的 skill。框架本身不提供原厂完整的语音交互和机器人行动能力，其他业务能力需要自行开发为 skill。**

- **不提供原厂完整的对话体验。** 当前语音入口用于技能指令识别、必要的参数澄清和结果播报，不提供通用闲聊、知识问答或原厂那样的连续多轮对话体验。长任务后的免唤醒接话仍有限制，见文末说明。
- **不内置通用机器人动作控制。** 起立、趴下、行走、转向、导航、跟随等指令，需要对应 skill 明确实现。框架不会将不支持的指令自动交给原厂 Agent 执行。
- **不会因接入大模型或 SDK 自动获得新能力。** 正式运行只调用已安装、已配置、已启用的 skill 所声明的动作；模型不能临时编写程序或任意调用机器人接口。未安装对应 skill 时，相应能力不可用。

例如，只安装点歌台时，可以请求播放歌曲；点歌时的起身、舞蹈、表情和灯光属于点歌台自身的联动逻辑，不代表框架支持独立的“起立”或“向前走”指令。需要其他能力时，应按本文的技能契约开发、安装并配置相应 skill；涉及机器人动作的 skill 还需自行实现状态检查、安全约束和执行结果确认。

## 快速开始

在 T1 **业务板 SoC 1** 上，克隆框架并安装：

```bash
git clone https://github.com/huang-kai/primebuddy.git primebuddy
cd primebuddy
source ./install.sh
```

应用就在这个目录中运行。安装向导会依次配置模型、选择随包技能，再调用各个技能自己的配置向导。方向键移动，空格多选，回车确认；允许暂不安装任何技能。Git 源码不包含构建产物或随包技能 ZIP；完成模型配置后，按下文安装 [PrimeSkills](https://github.com/huang-kai/primeskills) 中需要的技能，再启动平台。

日常使用：

```bash
primebuddy start
```

`start` 是前台运行命令。当前 T1 入口在设备预检通过后，将原厂 Agent 切换为 `only_voice` 并重启，由 PrimeBuddy 接管语音合成和技能调度；按 **Ctrl+C** 退出时恢复原模式。安装过程不会自动启动平台。技能的真实业务能力取决于已完成的配置，例如点歌台启用音乐联动后可能产生身体运动。

首次使用或新终端尚未加载机器人 ROS 环境时，先按本机固件提供的路径加载。已验证的 SoC 1 入口为：

```bash
source /robot/etc/basic_env
source /robot/software/housekeeper/bin/setup.bash
primebuddy start
```

更多环境说明见 [T1 与 SDK 接入](docs/sdk-compatibility.md)。框架不会自动安装 ROS、覆盖消息包或修改网络/DDS 配置。

## 设计架构

```text
安装与配置
  ZIP / Git → 清单和依赖检查 → skill.setup(ui, data_dir, config)
                                      ├─ 完成：保存配置并启用
                                      └─ 跳过：保留安装，等待配置

运行
  T1 语音输入 → ASR → LLM 生成计划 → 契约校验 → 任务调度 → skills
                       ↑                              ↓
           已就绪技能的清单、说明和上下文       结构化执行结果
                                                      ↓
                                                统一 TTS 反馈
```

| 层次 | 职责 | 关键代码 |
| --- | --- | --- |
| 命令与安装 | 平台入口、统一交互控件、ZIP/Git 获取、技能状态 | `__main__.py`、`onboarding.py`、`wizard.py`、`skill_manager.py` |
| 能力与调度 | 能力目录、LLM 计划、参数与依赖校验、并行执行、取消 | `registry.py`、`planner.py`、`engine.py`、`grounding.py` |
| 语音与模型 | 会话流程、ASR/LLM/TTS 配置、合成缓存 | `voice.py`、`models.py`、`voice_support/qwen_*.py`、`speech_cache.py` |
| T1 接入 | 原厂采音、播放、跨板连接、会话锁、Agent 模式恢复、ROS 兼容入口 | `voice_support/native_*.py`、`robot_audio.py`、`speech_audio.py`、`sdk_bridge.py` |
| 独立 skill | 自己的配置向导、登录、设备或服务调用、业务前置检查与清理 | 每个技能自己的 `src/` |

几个约定：

- 框架通过统一契约调用 skill，不按技能名称写业务分支。QQ 登录、二维码、歌曲时长、舞蹈以及灯具配置都属于对应 skill。
- 框架只提供通用终端控件。skill 可以用这些控件组合自己的向导，也可以在自己的源码中实现二维码等专用交互。
- 只有已安装、已配置、已启用的技能进入正式能力目录；清单和 `SKILL.md` 会提供给 LLM，无需重新训练模型。技能变化在下次启动时加载。
- LLM 只生成结构化任务；框架校验技能、动作、参数、依赖和资源冲突。它不执行 LLM 生成的 shell/Python 代码。
- 有依赖的任务等待前置任务成功完成；独立任务可以并行；占用同一资源的任务必须明确先后关系。
- 技能返回结果，框架统一播报。技能不自行接管 ASR/TTS、获取全局音频锁或切换原厂 Agent。

平台调度不依赖某个业务技能。当前语音入口仍直接接入 T1；这里没有为尚未实现的设备增加通用后端接口。后续接入其他设备时，应把设备生命周期集中在对应适配代码中，避免散落到技能或调度器。

## 代码结构

开发工作区建议如下；框架与每个技能可以分别建立 Git 仓库：

```text
workspace/
├── primebuddy/                       框架仓库
│   ├── src/primebuddy/
│   │   ├── __main__.py               命令解析与分发
│   │   ├── onboarding.py             主向导，调用技能自己的 setup
│   │   ├── wizard.py                 输入、单选、多选、标题与消息
│   │   ├── models.py / settings.py   模型预设、凭据与私有配置
│   │   ├── skill_manager.py          安装、升级、配置和启用状态
│   │   ├── registry.py               技能契约与能力目录
│   │   ├── planner.py / engine.py    计划生成、校验与执行
│   │   ├── grounding.py              真实名称核对与确认
│   │   ├── voice.py                  语音会话流程
│   │   ├── sdk_bridge.py             ROS ASR/TTS 兼容入口
│   │   └── voice_support/            模型请求、T1 音频与模式管理
│   ├── examples/                     调度计划示例
│   ├── tests/                       框架测试
│   ├── docs/                        接入说明与开发记录
│   ├── tools/build_release.py       发布打包工具
│   ├── install.py / install.sh       就地安装与命令注册
│   └── run.sh / test.sh              源码运行与验证入口
└── primebuddy-skills/                primeskills 仓库的本地目录
    ├── diangetai/                    官方点歌台：QQ 登录、二维码、播放与联动
    └── yeelight/                     易来：连接配置、灯控与状态核实
```

发布时，独立技能生成 ZIP 并放入框架发布包的 `bundled/`。随包技能和用户后续安装的技能使用同一条安装路径，框架源码中没有另一份技能实现。

## 安装与配置框架

### ZIP 或 Git

Git 安装见快速开始。框架仓库为 [huang-kai/primebuddy](https://github.com/huang-kai/primebuddy)，技能集合为 [huang-kai/primeskills](https://github.com/huang-kai/primeskills)。

也可以下载 [框架源码 ZIP](https://github.com/huang-kai/primebuddy/archive/refs/heads/main.zip)，在没有同名目录的位置解压并重命名：

```bash
python3 -m zipfile -e primebuddy-main.zip .
mv primebuddy-main primebuddy
cd primebuddy
source ./install.sh
```

GitHub 自动生成的源码 ZIP 与构建发布包不同：源码 ZIP 不含随包技能；使用本文打包工具生成的 `primebuddy-0.3.0.zip` 顶层目录固定为 `primebuddy/`，默认附带点歌台。当前许可证尚未选定，未附 LICENSE 文件。

安装器生成应用目录内的启动器与 `env.sh`，将 `~/.local/bin/primebuddy` 链接到启动器，并更新 Bash/Zsh 的 PATH 配置。使用 `source` 可以让当前终端立即获得命令；后续终端也可直接使用。应用目录移动后，在新位置重新运行安装器即可修复链接。

```bash
source ./install.sh --no-onboard  # 只注册命令，稍后再配置
primebuddy onboard              # 首次配置或重新进入向导
primebuddy --version
```

也可以运行 `python3 install.py`，但完成后当前终端需要执行一次 `source ./env.sh`。

| 安装参数 | 用途 |
| --- | --- |
| `--no-onboard` | 暂不进入交互向导 |
| `--data-dir PATH` | 数据目录，默认 `~/.local/share/primebuddy` |
| `--bin-dir PATH` | 命令目录，默认 `~/.local/bin` |

Git 来源需要系统 Git；技能声明第三方依赖时需要系统 Python 的 pip。依赖安装到各技能的 `_deps/`，不写入系统 Python。安装命令固定使用安装时的系统 Python，继承当前 shell 的 ROS 环境。

### 模型配置

目前提供 `qianwen` 一个服务商，ASR、LLM、TTS 共用一组 API Host 与 API Key：

| 能力 | 默认模型 | 说明 |
| --- | --- | --- |
| ASR | `qwen3-asr-flash` | 语音识别 |
| LLM | `qwen-plus` | 技能选择与任务计划 |
| TTS | `qwen-audio-3.0-tts-plus` | `longanlingxin` 女声，默认普通话 |

支持北京地域 `dashscope.aliyuncs.com` 或业务空间的 `*.cn-beijing.maas.aliyuncs.com`。ASR/LLM 使用对应 Host 的兼容接口；TTS 使用 `/api/v1/services/audio/tts/SpeechSynthesizer`，输出 T1 播放链路使用的 24kHz、单声道 PCM16 WAV。模型预设由框架统一管理，升级时沿用凭据；更换模型或默认音色后不会命中旧预设的缓存。

向导可分别检查三项云端能力，会产生少量 API 调用，不录音、不播放；可以跳过并稍后重新配置。密钥输入不回显，配置文件权限为 `600`。旧部署的 `qwen_asr.json` 保留只读兼容，新配置优先使用 `model.json`。

TTS 接入依据 [Qwen Audio HTTP 协议](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-http-api)和[音色列表](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list)。本版不提供方言或音色复刻配置。

## 安装和管理 skill

官方技能集合使用单技能 ZIP 安装。以点歌台为例，在框架目录之外执行：

```bash
git clone https://github.com/huang-kai/primeskills.git primebuddy-skills
git -C primebuddy-skills archive --format=zip --output=../diangetai-skill.zip HEAD:diangetai
primebuddy skill install ./diangetai-skill.zip
```

`primeskills` 的根目录是技能集合，不能直接作为单个技能安装。需要易来时，将 `HEAD:diangetai` 改为 `HEAD:yeelight` 并修改 ZIP 文件名；更多说明见 [PrimeSkills README](https://github.com/huang-kai/primeskills)。第三方单技能仓库仍可直接使用 Git 地址：

```bash
primebuddy skill install ./diangetai-skill-0.2.3.zip
primebuddy skill install https://example.com/hello-skill.zip
primebuddy skill install https://github.com/YOUR_ORG/hello-skill.git
primebuddy skill install https://github.com/YOUR_ORG/hello-skill.git --ref v0.1.0
primebuddy skill install git@github.com:YOUR_ORG/private-skill.git

primebuddy skill list
primebuddy skill catalog
primebuddy skill configure diangetai
primebuddy skill disable diangetai
primebuddy skill enable diangetai
```

上述网络地址均为示例。ZIP 可以直接包含 `manifest.json`，也可以包含一层顶层目录，兼容 GitHub 源码 ZIP。Git 仓库根目录须包含技能清单；`--ref` 可指定标签、分支或 commit。安装记录保存 ZIP 的 SHA-256 或实际 Git commit。私有 Git 使用现有 SSH 认证，不把密码或令牌写进 URL。

| 参数或命令 | 行为 |
| --- | --- |
| `install SOURCE` | 安装代码与依赖，随后调用技能自己的配置向导 |
| `install SOURCE --no-configure` | 只安装，状态为待配置，不向 LLM 暴露能力 |
| `install SOURCE --replace` | 升级已有技能；新版本配置成功后替换，失败保留旧安装 |
| `configure NAME` | 重新配置；开始前标记待配置，完成后启用 |
| `list` | 查看所有已安装技能的版本与状态 |
| `catalog` | 查看当前会提供给 LLM 的就绪能力目录 |
| `disable NAME` / `enable NAME` | 停用或启用；待配置技能不能直接启用 |

更新或重新配置前先退出平台会话，下次 `primebuddy start` 读取新代码和配置。`onboard` 会识别较新的随包版本并默认选中更新，普通第三方技能仍按自己的 ZIP/Git 来源安装升级。安装向导可取消或稍后继续，已完成的步骤保留；未勾选的已安装技能不会被卸载或停用。

点歌台自己的向导负责 QQ 扫码登录、播放时长和音乐联动。二维码的展示、私有图片与清理均由点歌台实现，框架不依赖 Pillow 或 QQ 音乐接口。播放行为与现场条件见点歌台自己的 README。

## 命令和启动参数

| 命令 | 用途 |
| --- | --- |
| `primebuddy` / `primebuddy --help` | 查看帮助，不启动设备 |
| `primebuddy --version` | 查看框架版本 |
| `primebuddy onboard` | 模型及技能配置向导 |
| `primebuddy start` | 启动平台并执行已启用技能，Ctrl+C 退出 |
| `primebuddy skill ...` | 技能管理 |
| `primebuddy simulate ...` | 开发时校验计划和模拟业务 |
| `primebuddy tts warmup` | 提前生成常用提示音缓存，不播放或切换模式 |

日常启动不需要附加参数。`start` 只保留两个有明确用途的高级选项：

| 参数 | 默认值 | 何时使用 |
| --- | --- | --- |
| `--sdk-bridge namespaced\|native\|off` | `namespaced` | 接入旧 ROS 客户端时选择 ASR/TTS 接口名称；不需要兼容接口时设为 `off` |
| `--asr-model MODEL` | `qwen3-asr-flash` | 仅在对比识别模型时临时指定；另一个支持值为 `qwen-audio-3.1-asr-flash`，不修改保存配置，也不自动回退 |

```bash
primebuddy start --help
primebuddy start --sdk-bridge off
primebuddy tts warmup --text '我在，请说。'  # --text 可重复；云端合成可能计费
```

启动检查跨板 SSH、音频服务和会话互斥锁；预检失败时不接管原厂 Agent。意外退出后，再次 `start` 会校验遗留恢复记录并直接接管 `only_voice`，不先恢复 `normal` 再接管。退出时保留最初的模式并核对恢复结果；记录损坏、其他会话占用或恢复未确认时给出错误，不丢弃恢复记录。

### ROS / SDK 接口

| 能力 | 默认 `namespaced` | `native` | 消息类型 |
| --- | --- | --- | --- |
| 最终识别文本 | `/primebuddy/asr_result` | `/aima/agent/asr_result` | `aimdk_msgs/msg/AsrResult` |
| 外部文本播报 | `/primebuddy/PlayTts` | `/aimdk_5Fmsgs/srv/PlayTts` | `aimdk_msgs/srv/PlayTts` |

`native` 检测到同名接口会退出，不会停止原厂节点抢占名称。外部 TTS 只支持空闲时普通播报；接受请求不等于已经播放完成。新技能应返回结构化结果，由平台统一反馈。

运行时使用 T1 已安装、已编译的 `rclpy` / `aimdk_msgs`；不要把它们写入技能 pip 依赖或用本地 SDK 覆盖原厂消息包。需要收发其他 topic/service 的技能可直接使用原生 ROS 类型，并自行管理节点与清理。详见 [SDK 兼容与迁移说明](docs/sdk-compatibility.md)。

## 开发一个 skill

```text
hello-skill/
├── manifest.json
├── SKILL.md          提供给 LLM 的能力说明、参数与使用约束
├── README.md         安装、配置和开发说明
└── src/
    ├── setup.py      技能自己的安装向导
    └── skill.py      业务入口；可用相对导入拆分私有模块
```

最小清单：

```json
{
  "name": "hello",
  "version": "0.1.0",
  "api_version": 1,
  "description": "问候用户",
  "setup": "src/setup.py",
  "entrypoint": "src/skill.py",
  "dependencies": [],
  "actions": {"hello": {"parameters": {}, "resources": []}}
}
```

`dependencies` 使用 `包名==固定版本`。`actions` 声明参数和所占资源；参数类型目前支持 `string`、`boolean`、`integer`，可用 `enum` 限定值。计划中的参数必须与清单一致。技能自身仍需实现跨字段业务校验。

### 安装向导

`src/setup.py` 提供 `setup(ui, data_dir, config)`：

```python
def setup(ui, data_dir, config):
    ui.info('问候技能配置')
    return {'greeting': ui.ask('问候语', config.get('greeting', '你好'))}
```

返回 `dict` 表示配置完成；返回 `None` 表示稍后配置。框架保存返回配置，技能可在自己的 `data_dir` 保存登录态等私有数据。安装时不启动业务或执行设备动作。

通用 `ui` 契约：

| 方法 | 作用 |
| --- | --- |
| `header(title, subtitle='')` | 标题 |
| `info(message)` | 消息 |
| `ask(label, default='', secret=False)` | 文本或隐藏输入 |
| `confirm(label, default=True)` | 是/否 |
| `choose(label, choices, default=0)` | 单选，返回下标 |
| `multiselect(label, choices, default=None)` | 多选，返回下标列表，允许为空 |

框架不提供 `ui.qr()` 或业务登录方法。需要二维码的技能在自己的模块中显示图片，并通过 `ui.info()` 输出提示，保留相同的向导风格。图片和登录临时文件由技能在成功、失败和取消时清理。

### 运行契约

| 函数 | 职责 |
| --- | --- |
| `validate(action, args)` | 参数业务校验；失败抛出异常 |
| `preflight(action, args, context)` | 检查执行条件；模拟时不能控制设备 |
| `describe(action, args)` | 返回简短的执行前播报文本 |
| `run(action, args, context)` | 执行业务，返回结构化结果 |
| `planning_context(context)`（可选） | 提供真实目标名称等规划上下文；`vocabulary` 列表可作为 ASR 热词 |
| `confirmation(action, args, text, snapshot)`（可选） | 核对用户原话和本技能上下文；返回非空字符串触发整轮确认，返回 `None` 表示无需确认 |

`context.config_for('hello')` 读取技能自己的配置；`context.simulate` 表示模拟执行；长任务定期调用 `context.checkpoint()` 响应取消，并在 `finally` 释放自己的资源。模拟是受信任技能遵守的代码契约，不是进程沙箱；技能不得在导入或模拟分支中调用设备。

任务示例：

```json
{"id":"one","skill":"hello","action":"hello","args":{},"after":[]}
```

执行结果示例：

```json
{"status":"success","message":"你好"}
```

状态支持 `success`、`failed`、`auth_required`、`clarification_required`；失败依赖会被框架标记为 `skipped`。任务已提交不能当作执行成功，应依据业务反馈返回结果。

可选确认扩展的调用与匹配逻辑见 `grounding.py` 及易来技能的 `confirmation()`；没有确认需求可以省略。技能在同一 Python 进程内运行，依赖版本需要兼容；只安装可信来源的代码。

## 开发验证与打包

开发工作区需将两个仓库放在相邻目录；`primebuddy-skills` 是 `primeskills` 仓库的本地目录名，以匹配默认测试和打包路径：

```bash
git clone https://github.com/huang-kai/primebuddy.git primebuddy
git clone https://github.com/huang-kai/primeskills.git primebuddy-skills
cd primebuddy
```

以下从框架源码仓库根目录执行。示例计划依赖同级 `primebuddy-skills/` 中的两个示例技能，但模拟执行不需要账号或 ROS：

```bash
bash test.sh
bash run.sh simulate --skills-dir ../primebuddy-skills --plan examples/parallel.json
bash run.sh simulate --skills-dir ../primebuddy-skills --plan examples/sequence.json
```

`simulate --plan FILE` 直接读取计划；`simulate --text TEXT` 会调用真实 LLM，再模拟技能。两者互斥。`--skills-dir` 只在模拟命令中可用，`start` 始终使用安装记录。

完整工作区测试还会运行同级独立技能的测试。SDK 桥接测试读取工作区同级 `sdk_t1-main/aimdk_msgs` 作为消息夹具；缺少 SDK 时会明确跳过，不需要为离线测试安装 ROS。

```bash
python3 tools/build_release.py --output /tmp/primebuddy-0.3.0.zip
python3 tools/build_release.py --skill-only ../primebuddy-skills/diangetai --output /tmp/diangetai-skill-0.2.3.zip
```

构建发布包时默认附带独立点歌台；可重复 `--skill PATH` 指定其他技能项目。打包工具只读取发布清单中的说明和源码目录，跳过 Python 缓存，不读取安装数据目录；账号、虚拟环境和运行数据应始终保留在源码之外。GitHub 仓库提交源码，`dist/` 构建产物不纳入 Git。

## 数据与升级

```text
~/primebuddy/                         实际应用，可自行选择父目录
├── src/                              框架源码
├── bundled/                          随包 skill ZIP
├── primebuddy                        安装时生成的启动器
└── env.sh                            安装时生成的 PATH 配置

~/.local/bin/primebuddy               指向应用启动器
~/.local/share/primebuddy/            可由 PRIMEBUDDY_HOME 覆盖
├── model.json                        私有模型凭据
├── skills.json                       技能来源、版本、配置与启用状态
├── packages/<name>-<id>/              已安装技能与 _deps/
└── data/<name>/                       每个技能的配置和登录数据
```

会话锁、模式恢复记录和语音缓存由 `PRIMEBUDDY_RUNTIME` 指定，默认使用 `~/.local/state/primebuddy`。旧 T1 部署若存在 `/mnt/user_packages/qqmusic/runtime`，会继续沿用该目录以保持历史音频锁和恢复记录一致；这不是点歌台登录配置目录。不要删除未完成的恢复记录来绕过检查。

### 一起更新框架和随包技能

先用 Ctrl+C 退出平台，保留数据目录，再将新发布包解压到新的应用目录：

```bash
python3 -m zipfile -e primebuddy-0.3.0.zip ./release-0.3.0
cd ./release-0.3.0/primebuddy
source ./install.sh
primebuddy --version
primebuddy skill list
primebuddy start
```

安装向导中可跳过模型配置，继续使用原 Key。技能列表会将较新的随包版本标为“更新至…”，默认选中；确认后安装新代码并调用新技能自己的向导，沿用已有登录态和配置。同版本只重新配置，已安装版本较新时不会降级；取消勾选则不更新。

本次发布为框架0.3.0和点歌台0.2.3，二者一起更新。点歌台的二维码流程已完全移入技能，不保留框架的旧 `ui.qr()` 接口。日常启动统一使用 `primebuddy start`；旧的 `--voice --live --allow-only-voice` 组合以及 `--setup`、`skills` 等冗余别名已删除。

## 已知限制与验证范围

- **长任务后的免唤醒接话尚未修复。** T1 原厂会话可能在任务期间超时进入空闲；PrimeBuddy 播报“还想做什么？”和打开本地窗口，不代表原厂已恢复收音。已观测到用户回答期间原厂音频包为零。临时方式是重新说“元仔元仔”，等回应后继续。仍需厂商提供本固件可用的续听接口或修正方案。
- 当前语音入口依赖 T1 原厂唤醒与采音，运行于 SoC 1，并通过 SSH 访问 SoC 0。原厂 `PlayMediaFile` 静音播放不能恢复已超时会话；有消息定义也不代表本机存在可调用服务。
- 0.3.0 与点歌台0.2.3已在 T1 部署，命令入口、技能加载、既有 QQ 登录态和系统 ROS 消息导入检查通过；新入口的完整语音与点歌流程仍待现场试用。0.2.5 的 Plus TTS 已在 T1 系统 Python 3.10.12 上完成实际云端合成校验，不能据此推断音质或所有设备动作已验收。
- 当前不提供后台守护、`stop` 命令或开机自启动；退出前台进程请使用 Ctrl+C。新增这些生命周期能力时再提供对应命令。

实现与回归记录见 [开发计划](docs/development-plan.md)。
