# PrimeBuddy

通过 Agent Skills 扩展能力的机器人应用平台。当前代码版本 1.0.0，使用 Python 3.10+；T1 语音入口运行在业务板 SoC 1，复用原厂 ROS 2 环境。框架提供 ASR、LLM 工具循环、TTS 和任务调度，业务由目录中的可用 skill 执行。

框架采用标准 `SKILL.md` 发现、按需读取和工具结果回传。普通 skill 不必提供清单、代码或五个 Python 契约函数。点歌台与易来使用可选原生工具适配，须和新框架一起重新打包安装；旧版 run 入口需迁为具名业务函数。

## 安装与启动

T1 业务板 SoC 1 先加载原厂环境：

```bash
source /robot/etc/basic_env
source /robot/software/housekeeper/bin/setup.bash
```

在框架源码或发布包解压目录中：

```bash
source ./scripts/install.sh
primebuddy start
```

安装器把框架复制到统一应用目录 `~/.primebuddy`，生成 `bin/primebuddy`、`bin/env.sh` 并配置终端 PATH。源码、技能、配置和运行数据都在该目录中；安装后原下载解压目录可以删除。优先复用系统 PyYAML；缺失时按根目录 `requirements.txt` 安装到应用 `_deps`，不创建虚拟环境。首次准备默认日志目录可能需要管理员权限。

安装向导先选择模型 provider，填写 API Host 和 API Key，再从该 provider 的分类候选列表选择 ASR、LLM、TTS 模型，最后验证连接并选择、配置技能。候选模型来自 `cfg/providers.yaml`；方向键移动，空格多选，回车确认。

也可以先安装，稍后配置：

```bash
python3 main.py install --no-onboard
source ~/.primebuddy/bin/env.sh
primebuddy onboard
```

`python3 main.py install --home PATH` 指定整个应用目录；未指定时读取 `PRIMEBUDDY_HOME`，再使用默认 `~/.primebuddy`。生成的命令绑定该目录，日常无需设置环境变量。CLI 中的相对文件路径仍相对于执行命令时的当前目录。安装时不会启动语音会话。框架使用系统 Python 和部署的 ROS 消息绑定，不从外部 SDK 编译或覆盖原厂消息包；固件升级后需要重新确认环境路径。

升级时先退出 PrimeBuddy，再从新版本解压目录执行同一安装命令。安装器暂存新代码和依赖后替换框架文件，保留实际配置、已安装技能、登录态和运行记录；替换失败会回滚。正在运行或有其他安装、配置任务时拒绝升级。RPK 使用 ZIP 解压方式，顶层目录叫 `primebuddy`，正式应用位于 `~/.primebuddy`；安装器不记录解压路径，安装后可删除解压目录。

`start` 前台运行，预检通过后将原厂 Agent 切换为 `only_voice` 并重启；Ctrl+C 退出时恢复原模式。点歌台开启联动后可能产生身体运动，使用条件见技能 README。

## 安装和管理 skill

直接把配置好的技能目录放到 `~/.primebuddy/skills/<name>/`，或应用目录 `.agents/skills/<name>/`，再重启会话即可发现。其他技能集合目录可通过私有 `cfg/config.yaml` 的 `skills.directories` 添加。最小技能只需要带 name、description 的 `SKILL.md`；代码和配置按需添加。

也可以直接安装一个本地目录：

```bash
primebuddy skill install ../primebuddy-skills/diangetai
```

通过本地目录、RPK、ZIP 或 Git 安装的技能统一放到 `~/.primebuddy/skills/<name>/`，例如 `skills/diangetai/`。升级继续使用同一目录，技能代码、资源和 `_deps` 放在其中；业务配置和登录数据分别保存在 `cfg/skills/<name>/` 和 `data/<name>/`，不会混入技能代码。

工作区的两个 skill 在相邻的 `primebuddy-skills` 中。从当前工作内容生成技能包：

```bash
python3 src/primebuddy/tools/build_release.py --skill-only ../primebuddy-skills/diangetai --output-dir /tmp/releases
primebuddy skill install /tmp/releases/diangetai-0.3.0.rpk
```

易来将路径换成 `../primebuddy-skills/yeelight`。技能集合根目录不能作为单个技能安装。单技能 RPK 或 ZIP 可以直接包含项目目录内容，也可以包一层顶层目录；单技能 Git 仓库支持 `--ref`。私有仓库沿用现有 SSH 认证。

```bash
primebuddy skill list
primebuddy skill catalog
primebuddy skill configure diangetai
primebuddy skill disable diangetai
primebuddy skill enable diangetai
primebuddy skill install /tmp/releases/diangetai-0.3.0.rpk --replace
```

`primebuddy skill list` 显示已发现技能的名称、版本、状态和实际目录，包括停用、待配置和缺少前置条件的技能。私有 `cfg/skills.yaml` 只保存技能目录、版本、配置完成和启用状态，不记录下载地址、包类型、Git ref、提交号或包指纹。旧记录的这些字段在后续安装、配置或启停保存时移除；直接复制到技能目录的内容也会被发现，执行配置或启停命令时保存管理状态。

启动时，框架从磁盘建立内存中的技能注册表。每轮向大模型提供已就绪、已启用且满足前置条件的技能名称和描述；模型选中技能后再读取完整 `SKILL.md` 和动作定义，按需查询上下文。`primebuddy skill catalog` 可查看提供给模型的目录摘要。技能安装、配置或启停后重启会话生效。

无需配置的技能安装后直接可用。声明了配置步骤的技能可使用 `--no-configure` 暂缓，稍后运行 `skill configure`；配置既可以是技能自己的 Console 脚本，也可以是可选 UI 向导。升级失败保留已有版本，重新配置中断标记待配置。直接复制的技能也能使用管理命令，变更后重启会话生效。

## 打包发布（RPK）

在框架源码目录运行：

```bash
python3 src/primebuddy/tools/build_release.py
```

默认打包框架及相邻 `primebuddy-skills` 集合中的所有 skill，分别生成独立发布包：

```text
dist/
├── primebuddy-1.0.0.rpk
├── diangetai-0.3.0.rpk
└── yeelight-0.2.0.rpk
```

文件名自动使用 `项目名-版本号.rpk`。框架版本读取 `src/primebuddy/__init__.py` 的 `__version__`；skill 名称和版本读取 `SKILL.md`、`cfg/pkg.yaml`，普通 skill 也可只用 `SKILL.md` 的 `metadata.version`。输出文件先生成 ZIP，再改为 `.rpk`，内容仍为 ZIP 格式，用于准备启元 SkillHub 发布包。

框架包内继续包含本次生成的 skill RPK，放在 `resource/bundled/`，安装向导可以选择它们；同一份 skill 包也单独输出用于发布。打包不会混入旧的随包版本，开发文档、测试目录、账号登录态、实际框架配置和运行数据不进入发布包。

仅打框架、仅打某个 skill 或指定输出目录：

```bash
python3 src/primebuddy/tools/build_release.py --framework-only
python3 src/primebuddy/tools/build_release.py --skill-only ../primebuddy-skills/diangetai
python3 src/primebuddy/tools/build_release.py --output-dir /tmp/releases
```

`--skill PATH` 可重复指定本次一起打包的技能。默认保留已有同名发布包；需要重新生成同版本包时加 `--overwrite`，或先更新项目版本。原来的 `--output 文件名` 改为 `--output-dir 目录`，无需手工填写名称或改后缀。

RPK 可以用普通 ZIP 工具解压；框架包的安装方式为：

```bash
unzip dist/primebuddy-1.0.0.rpk -d /tmp/primebuddy-install
cd /tmp/primebuddy-install/primebuddy
source ./scripts/install.sh
```

skill 包可直接使用 `primebuddy skill install dist/diangetai-0.3.0.rpk`，HTTPS RPK 下载地址也受支持；旧 `.zip` 包仍可安装。

## 开发一个 skill

普通 skill 最少只需要 `SKILL.md`，无需额外 Python 依赖、平台基类或五个契约函数。需要执行代码时添加普通脚本；需要配置、包元信息或共享机器人音频时再增加相应声明。以下命令从框架源码目录执行，示例技能放在相邻的 `my-skills/hello/`。

1. **建立技能目录，写清能力和触发条件。** 最小结构为：

   ```text
   my-skills/hello/
   └── SKILL.md
   ```

   `SKILL.md` 示例：

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

   `name` 使用小写字母、数字和单连字符，最多 64 字符；`description` 写明用途和何时使用。版本为可选项，不提供时使用 `0.0.0`。纯规则或说明类技能到这里就可以安装。

2. **需要业务操作时添加普通脚本。** 例如增加 `src/greet.py`：

   ```python
   import argparse

   parser = argparse.ArgumentParser()
   parser.add_argument('name')
   args = parser.parse_args()
   print('你好，' + args.name)
   ```

   将 `SKILL.md` 正文改为：

   ```text
   没有提供名字时，先询问名字。
   运行 src/greet.py，将名字作为一个命令行参数。
   根据脚本的实际输出回复用户。
   ```

   脚本不必导入 PrimeBuddy。支持 Python、Bash 和 JavaScript；打印文本即可，也可以输出包含 `status`、`message` 的 JSON 对象。非零退出码表示失败。业务脚本运行时不能交互读取输入，交互配置应放到下一步的配置入口。

3. **按业务需要声明依赖和配置。** 只用标准库时可省略 `requirements.txt`；需要第三方 Python 库时使用 `包名==固定版本`，安装器会安装到该技能的 `_deps/`。普通技能不必创建 `main.py` 或 `cfg/pkg.yaml`，配置、资源和主要测试按需要分别放在 `cfg`、`resource`、`tests`。

   需要安装时自动显示配置向导的技能，可在 `cfg/pkg.yaml` 声明：

   ```yaml
   primebuddy:
     setup: src/setup.py
   ```

   例如，一个需要设备地址的技能可以在 `src/setup.py` 实现：

   ```python
   def setup(ui, data_dir, config):
       address = ui.ask('设备地址', config.get('device_address', ''))
       return {**config, 'device_address': address}
   ```

   `skill install` 安装文件和依赖后调用这个函数，框架保存返回的字典到 `~/.primebuddy/cfg/skills/<name>/config.yaml`，配置完成后启用技能。`ui` 提供 `ask`、`confirm`、`choose` 等终端控件，`data_dir` 用于技能私有数据，`config` 是已有配置。仅放一个 `setup.py` 文件不会自动调用，必须声明入口。

   已有交互脚本也可以用 Console 配置入口：

   ```yaml
   primebuddy:
     configure: [src/configure.py]
   ```

   `configure` 与 `setup` 二选一。Console 脚本自行保存配置，正常退出表示配置完成；框架通过 `PRIMEBUDDY_SKILL_CONFIG`、`PRIMEBUDDY_SKILL_DATA` 传入私有配置文件和数据目录路径。私有 YAML 文件权限为 600，账号和登录态不要写入源码配置或发布包。

4. **先验证业务，再安装和检查发现结果。** 对上述脚本直接执行：

   ```bash
   python3 ../my-skills/hello/src/greet.py 张三
   primebuddy skill install ../my-skills/hello
   primebuddy skill list
   primebuddy skill catalog
   ```

   保留主要成功、错误输入和业务失败的测试；涉及长任务时还要验证取消与清理。已配置 LLM 后，可以用下面的命令校验模型是否选择技能、读取说明并生成任务；它会调用云端，模拟时不会执行脚本或控制设备：

   ```bash
   primebuddy simulate --skills-dir ../my-skills --text "向张三问好"
   ```

5. **需要时重新配置或替换版本。** 首次安装希望暂缓配置时，用 `--no-configure` 替代上一步的安装命令，稍后继续：

   ```bash
   primebuddy skill install ../my-skills/hello --no-configure
   primebuddy skill configure hello
   ```

   已有技能直接运行 `skill configure hello`。更新已有代码使用：

   ```bash
   primebuddy skill install ../my-skills/hello --replace
   ```

   安装、修改或配置后重启框架会话，重新建立技能注册表。

6. **发布时打包单个技能。** 文件名根据技能名称和版本自动生成：

   ```bash
   python3 src/primebuddy/tools/build_release.py \
     --skill-only ../my-skills/hello \
     --output-dir /tmp/releases
   primebuddy skill install /tmp/releases/hello-0.1.0.rpk
   ```

   RPK 可在另一台已安装 PrimeBuddy 的机器上使用；本机已有同名技能时安装命令加 `--replace`。需要重打同版本包时，打包命令加 `--overwrite`。

需要共享 `context.audio`、取消控制或调度资源时，再使用可选原生工具适配。例如在 `cfg/pkg.yaml` 声明：

```yaml
primebuddy:
  entrypoint: src/actions.py
  actions:
    greet:
      parameters:
        name: {type: string}
      resources: []
```

`src/actions.py` 只需提供具名业务函数：

```python
def greet(context, name):
    context.checkpoint()
    return '你好，' + name
```

框架用 `greet(context=context, name=...)` 调用。`validate`、`preflight`、`describe` 等额外钩子按实际需要提供。点歌台和易来采用这一适配方式；普通脚本技能使用前面的步骤即可。包版本、作者和描述等元信息可按启元风格写入 `cfg/pkg.yaml`，`package_name` 须与 `SKILL.md` 的 `name` 一致；`sdk_version` 是开发参考，不会要求每个技能安装机器人 SDK。

## 命令

| 命令 | 用途 |
| --- | --- |
| `python3 main.py --help` / `primebuddy --help` | 帮助，不启动设备 |
| `primebuddy --version` | 版本 |
| `primebuddy onboard` | 模型和技能配置向导 |
| `primebuddy start` | T1 语音会话 |
| `primebuddy skill ...` | 安装、配置和启停技能 |
| `primebuddy simulate ...` | 本地计划校验与模拟 |
| `primebuddy tts warmup` | 生成提示音缓存，不播放；未命中时调用云端 |

`start` 的高级参数为 `--sdk-bridge namespaced|native|off` 和 `--asr-model`。默认 SDK 入口为 `/primebuddy/asr_result`、`/primebuddy/PlayTts`；`native` 使用原厂命名，`off` 关闭兼容入口。

## 配置与资源

```text
PrimeBuddy/
├── main.py
├── README.md
├── requirements.txt    Python 依赖
├── cfg/                 公开 YAML 模板（*.example.yaml）
├── src/primebuddy/      按用途分组的代码
├── resource/           正式静态资源；发布时包含 bundled RPK 技能包
├── tests/              主要功能回归；测试录音在 voice，音频不提交 Git
├── docs/               本地开发文档，可选，暂不提交 Git
└── scripts/            安装、运行和测试脚本
```

框架的普通配置合并在 `cfg/config.yaml`，包括模型选择、运行目录、日志和缓存大小。provider 连接参数、凭据和分类候选列表单独在 `providers.yaml`，两类文件各有用途；其余原来的小配置文件已删除。

安装向导先配置一个 provider，再选择其 ASR、LLM、TTS 模型，结果使用 `provider:model`：

```yaml
asr:
  model: "qianwen:qwen3-asr-flash"
llm:
  model: "qianwen:qwen-plus"
tts:
  model: "qianwen:qwen-audio-3.0-tts-plus"
  tts_voice: longanlingxin
```

模型候选列表按 provider 在 YAML 中维护，调用协议和连接验证由 provider 代码实现。当前已接入千问，支持北京地域 `dashscope.aliyuncs.com` 或业务空间域名。运行配置仍支持三个能力分别引用已接入的 provider，手工修改各能力的 `model` 和对应 provider 记录即可。TTS 音色在 `config.yaml` 的 `tts.tts_voice` 设置，千问默认 `longanlingxin`。T1 的固定跨板地址在音频适配代码中维护。

源码 `cfg/config.example.yaml` 是公开默认值，`cfg/providers.example.yaml` 是包含候选模型、没有真实密钥的模板。首次安装创建实际 `config.yaml` 和 `providers.yaml`，向导再保存实际选择与凭据；升级只更新模板。以后改当前模型或音色编辑 `cfg/config.yaml`，改 Host、Key 或候选列表编辑 `cfg/providers.yaml`。候选列表按能力替换；旧配置没有 `models` 时使用公开模板的候选，不自动重写旧文件。重启会话生效。实际配置不提交 Git，也不进入发布包。

```yaml
# 私有 cfg/providers.yaml 示例；api_key 填自己的 Key。
qianwen:
  api_host: dashscope.aliyuncs.com
  api_key: ""
  models:
    asr:
      - qwen3-asr-flash
      - qwen-audio-3.1-asr-flash
    llm:
      - qwen-plus
    tts:
      - qwen-audio-3.0-tts-plus
```

默认安装后的统一目录：

```text
~/.primebuddy/
├── main.py                     框架主入口
├── README.md                   使用说明
├── requirements.txt            框架 Python 依赖
├── bin/
│   ├── primebuddy              日常命令
│   └── env.sh                  终端 PATH 设置
├── src/primebuddy/             框架源码
├── cfg/
│   ├── config.yaml              实际设置与模型选择
│   ├── providers.yaml           provider Host、Key 和分类候选模型
│   ├── skills.yaml              技能安装与启停记录
│   ├── *.example.yaml           随框架更新的公开模板
│   └── skills/<name>/config.yaml 技能业务配置
├── skills/<name>/              安装或直接复制的技能、资源和 _deps
├── data/<name>/                 登录态等技能私有数据
├── runtime/                     会话锁、恢复记录和语音缓存
├── resource/                    框架资源与随包技能 RPK
├── _deps/                       系统缺少时安装的 Python 依赖
└── scripts/                     安装和开发脚本
```

技能和其私有配置、登录数据在安装或配置后生成；`_deps` 只在缺少依赖时创建。日志默认单独保存在 `/var/log/primebuddy/primebuddy.log`。日常修改实际 `cfg/config.yaml`、`cfg/providers.yaml` 和 `cfg/skills/<name>/config.yaml`；`*.example.yaml` 是公开模板，`skills.yaml` 由程序维护。

旧 `model.yaml` 在首次加载模型时校验并拆分成 `config.yaml` 和 `providers.yaml`；已有新文件优先，不覆盖它们。旧模型尚未接入或旧配置无效时明确报错，保留旧文件。`skills.yaml` 和旧的 `data/<name>/config.yaml` 仍迁入私有 `cfg`；QQ 登录态、音频锁和恢复记录留在原位置。`skills.yaml` 是程序维护的安装状态，不属于手工模型配置。

私有 YAML 权限为 600，目录为 700；手工修改后重启生效。YAML 重复键、非法类型和缩进错误会报错，不输出配置正文。运行向导重新保存时会重建 YAML，不保留手工注释。

会话状态和缓存目录由 `config.yaml` 的 `runtime.directory` 设置，默认值 `runtime` 相对于统一应用目录，即 `~/.primebuddy/runtime`；也可填写绝对路径或用 `PRIMEBUDDY_RUNTIME` 覆盖。不要删除未完成恢复记录来绕过检查。安装器不自动搬迁此前分散在其他目录的部署数据。

## 日志与排查

运行日志默认在 `/var/log/primebuddy/primebuddy.log`，可用 `PRIMEBUDDY_LOG_DIR` 覆盖。框架与两个 skill 使用标准库 logging，文件包含时间、级别、模块名；默认每份约 10 MiB，保留 5 份轮转文件，权限为 600。日志写盘失败时继续终端输出与退出清理。

```bash
tail -f /var/log/primebuddy/primebuddy.log
```

日志记录识别文字和业务阶段，不输出账号凭据或签名资源 URL。终端向导、命令结果以及 JSON 通信协议保持独立，不通过标准输出拦截写日志。

## 开发与限制

```bash
bash scripts/test.sh
python3 main.py simulate --skills-dir ../primebuddy-skills --plan tests/fixtures/parallel.json
```

`simulate --text` 使用已配置的云端模型验证 Agent 工具循环；`--plan` 仍用于离线校验确定的任务清单。固定脚本在模拟时不会启动，原生模拟由可信业务代码遵守约定。执行现有脚本不是限制不可信代码访问系统的沙箱。

当前语音入口用于技能指令和必要澄清，不提供通用闲聊或未注册的机器人动作。尚不支持实时语音打断、短停顿合句或多人说话人锁定；首次使用及空闲超时后需原厂唤醒。退出使用 Ctrl+C，当前没有后台守护、stop 命令或开机自启动。

`docs/` 及各 skill 的开发文档暂时只保留在本地，框架安装器不复制文档目录；GitHub 克隆、安装和打包不要求这些文件存在。主要测试数据在 `tests/fixtures/`，测试录音仅保留在本地。

源码仓库：[PrimeBuddy](https://github.com/huang-kai/primebuddy)、[PrimeSkills](https://github.com/huang-kai/primeskills)。许可证尚未选定。
