# 网络故障提示音

`network-error.wav` 使用 Q1 原厂 `check_internet.wav`，来源为本次连接设备的 `/robot/software/aimrt_agent/bin/cfg/q1/audio/check_internet.wav`。原文件为 16 kHz 单声道 PCM16 WAV，时长约 3.275 秒，SHA-256：

```text
de163ff8a047a2350aa1a01b840ad310cd9f8f2ead9a95e4d8ac58979293d377
```

打包资源预先转为框架提示音统一使用的 24 kHz 单声道 PCM16 WAV，运行时再按现有原厂媒体接口转换、播放。T1/Q1 均读取随包资源，不引用上述原厂安装路径，不要求其他控制板存在同名文件。

代码中的 `NETWORK_ERROR_PROMPT` 用于选择该资源，不代表原厂录音的逐字文案。故障时无需云端 TTS 或重新下载音频。
