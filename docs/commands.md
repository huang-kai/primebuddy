# 命令参考

在实际安装 Buddy 的板上运行。`primebuddy --help` 和各子命令 `--help` 可查看当前版本参数。

| 命令 | 参数 | 用途 |
| --- | --- | --- |
| `primebuddy --version` | 无 | 显示平台版本 |
| `primebuddy onboard` | 无 | 机器人连接、模型配置、可选技能安装和配置 |
| `primebuddy start` | `--asr-model provider:model` | 仅覆盖本次会话的 ASR，不修改配置，失败不自动回退 |
| `primebuddy start` | `--sdk-bridge namespaced\|native\|off` | SDK 桥接模式，默认 namespaced；native 会使用原厂命名空间，应理解冲突后使用 |
| `primebuddy update` | `--check` | 只检查版本与兼容性，不安装 |
| `primebuddy update` | `--version 版本号` | 指定正式目标版本，不执行降级 |
| `primebuddy update` | `--recover` | 本地恢复未完成升级，不联网；与 --check 互斥 |
| `primebuddy skill list` | 无 | 已发现技能的版本、状态与目录 |
| `primebuddy skill catalog` | 无 | 可提供给模型的能力目录 JSON |
| `primebuddy skill install 来源` | `--ref Git引用`、`--no-configure`、`--replace` | 安装或替换技能；来源可为本地目录、RPK/ZIP、HTTPS 包或 Git 地址 |
| `primebuddy skill configure 名称` | 无 | 调用技能自己的向导 |
| `primebuddy skill enable 名称` | 无 | 检查兼容与就绪状态后启用 |
| `primebuddy skill disable 名称` | 无 | 停用并保留文件和数据 |
| `primebuddy simulate` | `--skills-dir 目录`，`--plan 文件` 或 `--text 文本` | 开发模拟；plan/text 互斥，文本可能调用云模型，不执行真实机器人动作 |
| `primebuddy tts warmup` | `--text 文本`，可重复 | 预生成提示音缓存，单条 1～100 字；可能调用云端，不播放或接管机器人 |

技能安装、替换与配置的完整例子见[技能管理](skill-management.md)。删除技能及数据保留策略也在那里说明；打包脚本不是上述运行时 CLI，见[打包发布](packaging.md)。

安装脚本支持 `source ./scripts/install.sh --no-onboard`，先完成安装，稍后用 `primebuddy onboard` 配置。启动后按 Ctrl+C 退出并等待原厂模式恢复。

[返回文档导航](README.md) · [返回项目首页](../README.md)
