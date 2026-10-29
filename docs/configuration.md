# 配置与文件位置

默认安装目录如下。技能、配置和登录数据会在安装或配置后生成。

```text
~/.primebuddy/
├── main.py                  程序入口
├── README.md                使用说明
├── docs/                    分类使用说明与协议文档
├── CHANGELOG.md             版本更新记录
├── release.json             当前框架版本及官方技能兼容清单
├── LICENSE                  MIT 开源协议
├── requirements.txt         框架 Python 依赖
├── bin/                     primebuddy 命令和终端环境脚本
├── src/primebuddy/           框架代码
├── cfg/
│   ├── config.yaml          模型选择、运行目录和日志设置
│   ├── providers.yaml       模型服务的 Host、Key 和候选模型
│   ├── skills.yaml          技能安装和启停记录
│   ├── *.example.yaml       公开配置模板
│   └── skills/<name>/       每个技能的业务配置
├── skills/<name>/           每个技能的代码、资源和依赖
├── data/<name>/             每个技能的私有数据，例如 QQ 登录态
├── runtime/                 会话锁、恢复记录和语音缓存
├── resource/                静态资源及可选的随包技能
├── _deps/                   框架需要额外安装的 Python 依赖
└── scripts/                 安装和开发脚本
```

日志默认保存在 `/var/log/primebuddy/primebuddy.log`。需要自定义安装位置时，使用 `source ./scripts/install.sh --home /你的安装目录`，下文的默认路径也应相应替换。

日常配置主要修改这三个位置：

| 想修改什么 | 文件 |
| --- | --- |
| 当前使用的模型、TTS 音色、日志设置 | `~/.primebuddy/cfg/config.yaml` |
| 模型服务的 API Host、API Key、候选模型 | `~/.primebuddy/cfg/providers.yaml` |
| 某个技能的业务设置 | `~/.primebuddy/cfg/skills/<name>/config.yaml`，也可运行 `skill configure` |

模型使用 `provider:model` 表示，例如：

```yaml
# cfg/config.yaml 中的模型部分
asr:
  model: "qianwen:qwen3-asr-flash"
llm:
  model: "qianwen:qwen-plus"
tts:
  model: "qianwen:qwen-audio-3.0-tts-plus"
  tts_voice: longanlingxin
```

对应的服务连接信息放在 `providers.yaml`：

```yaml
qianwen:
  api_host: dashscope.aliyuncs.com
  api_key: "填写自己的 API Key"
  models:
    asr: [qwen3-asr-flash]
    llm: [qwen-plus]
    tts: [qwen-audio-3.0-tts-plus]
```

`models` 是安装向导的候选列表。增加新的 provider 还需要在 `providers/` 中实现调用适配；只填写一个新名字不会自动接入服务。

手工修改后，重新启动语音会话生效。`*.example.yaml` 是公开模板，日常使用修改实际配置文件即可。真实 API Key 和登录数据不要提交到代码仓库或放进发布包。

[返回文档导航](README.md) · [返回项目首页](../README.md)
