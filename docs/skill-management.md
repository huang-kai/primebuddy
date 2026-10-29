# 部署和管理 skill

### 安装技能

把技能的 RPK 上传到机器上，在包所在目录执行：

```bash
primebuddy skill install ./diangetai-0.4.0.rpk
```

也可以安装本地的单个技能源码目录：

```bash
primebuddy skill install ./primebuddy-skills/diangetai
```

目录中应有 `SKILL.md`，不能把包含多个技能的集合根目录当成一个技能安装。安装器会复制文件、安装声明的 Python 依赖，并调用技能自己的配置向导。无需配置的技能安装后直接启用。

RPK 使用 ZIP 格式，旧 ZIP 包也支持；还可以传入 HTTPS 包下载地址，或根目录包含 `SKILL.md` 的单技能 Git 仓库地址。

需要以后再配置时：

```bash
primebuddy skill install ./diangetai-0.4.0.rpk --no-configure
primebuddy skill configure diangetai
```

### 更新、配置和停用

```bash
# 更新已有技能
primebuddy skill install ./diangetai-0.4.0.rpk --replace

# 重新配置
primebuddy skill configure diangetai

# 暂时停用，保留代码、配置和登录态
primebuddy skill disable diangetai

# 重新启用
primebuddy skill enable diangetai
```

安装、更新、配置或启停后，重新启动语音会话，让框架重新加载技能。需要一起更新框架与已安装的官方技能时，优先使用 `primebuddy update`；单独安装技能也会检查它与当前框架的兼容性。

### 删除技能

当前版本还没有 `skill uninstall` 命令，需要手动删除。以删除 `hello` 为例：

1. 按 Ctrl+C 退出正在运行的 PrimeBuddy。
2. 用 `primebuddy skill list` 确认 `hello` 的实际目录。在 `~/.primebuddy/cfg/skills.yaml` 中删除整个 `hello:` 条目，包括其下面缩进的内容；如果没有这个条目，跳过此步。
3. 删除技能代码目录。默认安装位置使用下面的命令；如果列表显示的是其他目录，以实际位置为准。

```bash
rm -rf ~/.primebuddy/skills/hello
primebuddy skill list
```

删除代码后，原来的配置和登录数据仍会保留。如果也要彻底清除该技能的数据，再执行：

```bash
rm -rf ~/.primebuddy/cfg/skills/hello ~/.primebuddy/data/hello
```

清除数据后重新安装，需要重新配置和登录。如果只是暂时不用，使用 `skill disable` 即可。

[返回文档导航](README.md) · [返回项目首页](../README.md)
