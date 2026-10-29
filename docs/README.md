# 文档目录

新用户从[项目首页](../README.md)完成快速部署，再按需要阅读：

| 分类 | 文档 | 内容 |
| --- | --- | --- |
| 设计 | [架构设计](architecture.md) | 平台与技能分工、CLI 进程边界、选择这些设计的原因 |
| 设计 | [音频流程](audio-flow.md) | only_voice、原厂唤醒与状态、task 和独占游戏、IDLE、异常交接 |
| 代码 | [项目结构](project-structure.md) | 代码模块与仓库目录职责 |
| 使用 | [安装部署](installation.md) | 环境、单板与双板、SSH、向导、启动退出 |
| 使用 | [命令参考](commands.md) | 子命令与参数 |
| 使用 | [配置说明](configuration.md) | 模型、环境变量、配置与日志路径 |
| 使用 | [技能管理](skill-management.md) | 安装、替换、配置、启用、禁用、删除 |
| 开发 | [技能运行协议](skill-protocol.md) | SKILL.md、CLI、task/exclusive、进程生命周期和资源 |
| 开发 | [安装向导协议](installation-protocol.md) | primebuddy.version、configure、路径和退出码 |
| 维护 | [升级与兼容](upgrades.md) | 独立升级、失败恢复、不兼容技能的当前处理和后续方案 |
| 维护 | [打包发布](packaging.md) | 组合包、独立包、随包清单、正式发行要求 |
| 贡献 | [贡献指南](contributing.md) | 开发环境、测试、PR、日志与许可 |

建议阅读顺序：使用者阅读首页 → 安装部署 → 命令与配置；技能作者阅读架构 → 安装向导协议 → 技能运行协议 → 打包发布。音频接管和机器人问题先阅读音频流程。

文档描述当前源码；标为“后续方案”的内容尚未实现。发行版本和验收范围见 [CHANGELOG](../CHANGELOG.md)。
