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

包默认输出到框架 `dist/`，名称为 `项目名-版本号.rpk`，例如 `primebuddy-1.1.0.rpk`。内容是 ZIP，打包工具会生成 ZIP 后改为 RPK 后缀，可用于分发或准备启元 SkillHub 发布包。

框架版本来自 `src/primebuddy/__init__.py`，技能版本来自 `cfg/pkg.yaml`；没有该文件时，使用 `SKILL.md` 中的 `metadata.version`，未声明版本时为 `0.0.0`。发布前请更新对应版本。

同时维护仓库根目录的 `release.json` 和 [CHANGELOG.md](../CHANGELOG.md)。发布清单使用 schema 版本 1，包含框架 `version`，以及各官方技能的 `min_version`、不含上界的 `max_version` 和固定 `recommended_version`。兼容范围表示本框架支持的版本，推荐版本是本次发布选择的具体包；不要把任意仓库的最新包自动视为兼容。

准备正式发行时从技能仓库收集兼容清单里的推荐版本：

```bash
python3 src/primebuddy/tools/build_release.py --framework --release --skills-dir ../primeskills
```

`release.json` 的 `bundled_skills` 明确指定默认大包只附带点歌台和成语接龙。`skills` 是更大的兼容与下载目录，易来仍可生成独立包供另行安装，但不放入默认大包。自定义非正式组合包仍按显式 `--skill` 选择内容。

此命令输出四个 RPK，以及带包下载地址与 SHA-256 的 `release.json` 和含发布日期及唯一兼容表的 `release-notes.md`。GitHub Release 应一并上传生成的清单和对应 RPK，发布说明使用生成内容，按 `Features`、`Fixes`、`Compatibility`、`Upgrade notes` 和 `Known issues` 组织；源码中的声明、包内声明和发布说明应保持一致。正式更新只使用发布清单中固定的包，不从开发分支直接拉取代码。仅打包成功不代表已经发布，也不代表实机验收通过。

`--output-dir 目录` 可以指定输出位置。修改源码后需要重打同版本包时增加 `--overwrite`；默认不会覆盖已有同名包。docs 用户与开发文档随包分发；测试文件、本地 design 草稿、实际框架配置和运行数据不会进入发布包；技能作者应把私有配置和登录态保存在框架的数据目录中。

大包包含技能源码、资源及固定依赖声明，Python 依赖仍需软件源，并非完全离线包。新版本发布前必须同步更新框架版本、技能版本、兼容清单和 CHANGELOG；本次修改仍在 Unreleased，不要用旧版本号覆盖已有正式发行。

[返回文档导航](README.md) · [返回项目首页](../README.md)
