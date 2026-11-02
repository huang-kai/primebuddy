# 打包发布

所有打包命令都在框架源码目录执行。直接运行脚本会显示帮助：

```bash
python3 src/primebuddy/tools/build_release.py
```

只打框架：

```bash
python3 src/primebuddy/tools/build_release.py --framework
```

只打指定技能：

```bash
python3 src/primebuddy/tools/build_release.py --skill ../primebuddy-skills/diangetai
```

将技能一起放进框架包，每个技能单独写一个 `--skill`：

```bash
python3 src/primebuddy/tools/build_release.py --framework --skill ../primeskills/diangetai --skill ../primeskills/chengyu-chain
```

只有显式指定的技能才会进入框架包。组合打包时，也会输出每个技能的独立 RPK。`--skill` 接受源码目录，已有 RPK 直接安装或发布即可。

包默认输出到框架 `dist/`，名称为 `<项目名>-<版本>.rpk`。内容是 ZIP，打包工具会生成 ZIP 后改为 RPK 后缀，可用于分发或准备启元 SkillHub 发布包。文档中的占位符需替换为实际项目名和版本号。

框架包根目录直接包含 `main.py`、`cfg/pkg.yaml`、`src/`、`resource/` 等文件和目录，不再套一层 `primebuddy/`。解压后直接进入解压目录执行安装脚本；在线更新仍兼容旧版带外层目录的包。

框架打包版本来自 `src/primebuddy/__init__.py`，框架 `cfg/pkg.yaml` 和 `release.json` 中的版本应与其一致。技能版本来自各自的 `cfg/pkg.yaml`；没有该文件时，使用 `SKILL.md` 中的 `metadata.version`，未声明版本时为 `0.0.0`。

同时维护仓库根目录的 `release.json` 和 [CHANGELOG.md](../CHANGELOG.md)。发布清单使用 schema 版本 1，包含框架 `version`，以及各官方技能的 `min_version`、不含上界的 `max_version` 和固定 `recommended_version`。兼容范围表示本框架支持的版本，推荐版本是本次发布选择的具体包；不要把任意仓库的最新包自动视为兼容。

准备正式发行时从技能仓库收集兼容清单里的推荐版本：

```bash
python3 src/primebuddy/tools/build_release.py --framework --release --skills-dir ../primeskills
```

`release.json` 的 `bundled_skills` 指定标准完整包中附带的技能。`skills` 是兼容与下载目录，其中的技能均生成独立包，但不一定放入完整包。自定义非正式组合包按显式 `--skill` 选择内容。

此命令生成两种平台安装包，以及 `skills` 中声明的各技能独立包，平台文件名如下：

| 文件 | 内容 |
| --- | --- |
| `primebuddy-<版本>.rpk` | Buddy 与 `bundled_skills` 中声明的技能；用户在向导中自行选择安装 |
| `primebuddy-<版本>-core.rpk` | 相同的 Buddy 代码和文档，不附带任何技能；可稍后单独安装技能 |

`release.json` 的 `framework` 指向标准完整包，`framework_core` 标识纯平台包，均包含下载地址和 SHA-256；在线更新默认使用标准完整包，不自动安装技能。`release-notes.md` 包含本版本更新说明和兼容表。`SHA256SUMS` 覆盖全部 RPK、清单和更新说明，可以在输出目录执行 `sha256sum -c SHA256SUMS` 验证。

GitHub Release 应一并上传生成的产物，发布说明使用生成内容；源码中的声明、包内声明和发布说明应保持一致。正式更新只使用发布清单中固定的包，不从开发分支直接拉取代码。仅打包成功不代表已经发布，也不代表实机验收通过。

`--output-dir 目录` 可以指定输出位置。修改源码后需要重打同版本包时增加 `--overwrite`；默认不会覆盖已有同名包。docs 用户与开发文档随包分发；测试文件、本地 design 草稿、实际框架配置和运行数据不会进入发布包；技能作者应把私有配置和登录态保存在框架的数据目录中。

完整包包含随包技能源码、资源及固定依赖声明，Python 依赖仍需软件源，并非完全离线包。发布时同步更新框架版本声明、发行清单和 CHANGELOG；技能有变更时再更新对应技能的版本与日志。发布说明和兼容表由打包工具从 CHANGELOG、`release.json` 生成。日常使用文档保持通用命令和包名占位符，具体发行版本、配套版本及验收结果集中记录在清单、CHANGELOG 和对应发布记录中。

[返回文档导航](README.md) · [返回项目首页](../README.md)
