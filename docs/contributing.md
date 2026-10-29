# 参与贡献

欢迎修复问题、改进文档或开发新技能。框架代码提交到 [PrimeBuddy](https://github.com/huang-kai/primebuddy)，技能的改动提交到 [PrimeSkills](https://github.com/huang-kai/primeskills)。

1. 在相应仓库创建 Issue，说明问题或想增加的功能。小的文档修正可以直接提交 PR。
2. Fork 仓库，在自己的分支上修改，尽量让一个 PR 解决一个问题。
3. 运行相关测试。行为变化应补充主要用例，文档改动应核对命令和路径。
4. 向仓库的 `main` 分支提交 PR，说明解决了什么问题、怎么修改，以及实际做了哪些验证。

在电脑上准备框架开发环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
bash scripts/test.sh
```

测试脚本会运行框架测试；如果相邻目录有 `primebuddy-skills`，也会运行各个技能的测试。测试不要求连接真实机器人，缺少原厂 SDK 的相关夹具会明确跳过。

源码目录中，`cfg/` 放公开配置模板，`src/` 放代码，`resource/` 放正式资源，`tests/` 放主要测试，`scripts/` 放安装和开发脚本。`docs/` 是随源码和安装包一起分发的用户与开发者文档，需要提交 Git。测试录音、生成的发布包、API Key 和账号登录态也不提交。

报告问题时，请附上版本、复现步骤和必要的日志，隐去密钥和账号信息。涉及机器人运动的验证，应单独说明测试环境和安全条件。

技能若占用扬声器，在 Buddy 侧适配的 `resources` 中声明 `speaker.music`（或 `speaker`）。框架统一把 `speaker.*` 视为同一资源，在音乐任务真实完成前推迟其他任务的语音播报，同时继续处理 ROS 事件。skill 必须等待原厂媒体自然结束及有界清理后返回，不能把播放请求已接受当作执行成功。业务程序在正常退出和标准信号处理过程中关闭自己持有的客户端；框架不需要了解其 SDK 实现。

[返回文档导航](README.md) · [返回项目首页](../README.md)
