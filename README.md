# PrimeBuddy

PrimeBuddy 是面向启元 T1 / Q1 的语音技能平台：用户说出请求，Buddy 识别、理解并调用技能。平台负责语音入口、任务调度和进程管理，技能负责自己的业务、配置向导及资源清理。

技能由独立的 [PrimeSkills](https://github.com/huang-kai/primeskills) 仓库维护，常用技能包括：

| 技能 | 功能 |
| --- | --- |
| 点歌台 `diangetai` | QQ 音乐登录、搜歌与播放，可配置动作、表情和灯光联动 |
| 成语接龙 `chengyu-chain` | 独占式语音游戏，自己处理答题、解释和退出，结束后交回 Buddy |

安装向导默认不选技能，空格可勾选多个，回车后依次进入各技能自己的安装和配置向导；选择 `None` 可跳过技能安装，之后运行 `primebuddy onboard` 可补装。完整包的随包技能以 [release.json](release.json) 中的 `bundled_skills` 为准，其他技能可单独安装。版本变化见 [更新记录](CHANGELOG.md)，本机版本可用 `primebuddy --version` 查看。

## 快速部署

发行提供两种安装包：`primebuddy-<版本>.rpk` 是含随包技能的完整包，`primebuddy-<版本>-core.rpk` 是不附带技能的纯平台包。两者安装步骤相同；将 `<版本>` 替换为所下载包的版本号，详见[打包与产物说明](docs/packaging.md)。

在机器人上使用系统 Python 3.10+、原厂 ROS 2 和设备随附的消息包；准备网络及模型服务 API Key。详细板型选择与 SSH 说明见[安装部署](docs/installation.md)。

### 通过 PrimeConsole 部署

在 PrimeConsole 中导入 RPK 并点击「部署到机器人」，等待安装结果显示通过。PrimeConsole将包解压到 `/home/user/lumina/packages/primebuddy`，根目录直接包含 `main.py`、`cfg/pkg.yaml` 和 `scripts/install.sh`。

部署通过后，还需要通过 SSH 执行 Buddy 的安装脚本，准备依赖、注册命令并完成配置：

```bash
ssh run@<机器人IP>
cd /home/user/lumina/packages/primebuddy
source ./scripts/install.sh
```

这一步与从源码或解压 RPK 安装相同，只是安装源目录不同。Console 的包目录用于存放部署文件；Buddy 默认实际安装到当前用户的 `~/.primebuddy`，例如 `run` 用户的 `/home/run/.primebuddy`。仅在 Console 部署新包，不会自动更新这份已有安装。

首次安装会自动进入向导，配置机器人连接、模型服务和可选技能。已有安装会保留配置、登录数据及技能启用状态，不自动重跑向导；需要重新配置或补装随包技能时，运行 `primebuddy onboard`。双板设备按安装器提示，在实际安装 Buddy 的板上执行后续命令。

```bash
primebuddy --version       # 确认已切换到新部署包的版本
primebuddy skill list      # 查看技能及配置状态
primebuddy start           # 启动语音会话
```

### 从源码或解压 RPK 安装

从源码安装：

```bash
git clone https://github.com/huang-kai/primebuddy.git primebuddy
cd primebuddy
source ./scripts/install.sh
```

源码安装不自动获取随包技能。若使用含技能的 Release 大包，解压后执行相同脚本；将下列文件名换为实际下载的版本：

```bash
unzip "primebuddy-<版本>.rpk" -d ./primebuddy-release
cd primebuddy-release
source ./scripts/install.sh
```

向导依次检查机器人连接、配置模型、让你选择技能并调用各技能自己的向导。默认安装到 `~/.primebuddy`。运控板发现大脑板时会转到大脑板安装；单板 Q1 在本板安装。后续在实际安装板上操作。

## 开始使用

```bash
primebuddy skill list
primebuddy start
```

说原厂唤醒词，等唤醒应答结束后再说“播放一首歌”或“开始成语接龙”。游戏独占期间 Buddy 暂停自己的语音处理，游戏结束并确认清理完成后再接管。按 `Ctrl+C` 退出 Buddy，等待原厂 Agent 恢复；不会因此关机。

```bash
primebuddy onboard                    # 继续配置或补装随包技能
primebuddy skill configure diangetai  # 重新配置技能
primebuddy skill disable diangetai    # 停用，保留配置与数据
primebuddy update --check             # 只检查更新
primebuddy update                     # 更新平台，保留兼容的技能版本
```

命令找不到时运行 `source ~/.primebuddy/bin/env.sh`。更多参数见[命令参考](docs/commands.md)；故障处理见[升级与恢复](docs/upgrades.md)。

## 文档导航

| 想了解什么 | 阅读文档 |
| --- | --- |
| 平台架构、职责边界、设计原因 | [架构设计](docs/architecture.md) |
| only_voice、唤醒、播报、收音、IDLE 与游戏交接 | [音频流程与设计原因](docs/audio-flow.md) |
| 代码如何组织、各目录做什么 | [项目结构](docs/project-structure.md) |
| 板型、安装位置、SSH、启动和退出 | [安装部署](docs/installation.md) |
| 命令与参数 | [命令参考](docs/commands.md) |
| 模型、环境变量、配置和日志路径 | [配置说明](docs/configuration.md) |
| 单独安装、配置、启停和移除技能 | [技能管理](docs/skill-management.md) |
| 开发技能、运行模式、进程与资源契约 | [技能运行协议](docs/skill-protocol.md) |
| 自定义安装向导、入口、返回值与版本 | [安装向导协议](docs/installation-protocol.md) |
| 平台升级、兼容性、失败恢复及不兼容方案 | [升级与兼容](docs/upgrades.md) |
| 大包、独立包、部署与 Release 产物 | [打包发布](docs/packaging.md) |
| 已记录版本的验证范围与遗留项 | [发布验收记录](docs/release-validation.md) |
| 开发环境、测试与提交规范 | [贡献指南](docs/contributing.md) |

完整阅读路径见 [docs 文档目录](docs/README.md)。

## 参与贡献

欢迎提交问题、修复和新技能。框架改动提交到本仓库，技能改动提交到 [PrimeSkills](https://github.com/huang-kai/primeskills)。PR 请说明问题、行为变化和实际验证结果；日志中隐去密钥和账号信息。开发环境及测试命令见[贡献指南](docs/contributing.md)。

## 开源协议

PrimeBuddy 采用 [MIT 协议](LICENSE)。分发时保留版权及许可声明；独立技能与第三方依赖遵循各自协议。
