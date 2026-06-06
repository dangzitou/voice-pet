# voice-pet

树莓派本地桌宠 MVP。这个项目故意放在 `~/.picoclaw/voice-pet/`，与 PicoClaw 仓库解耦，只复用 MiMo API 与后续可插拔的 BrainAdapter 思路。

## 当前阶段

- 已实现：
  - MiMo ASR / TTS / 文本对话适配器
  - `arecord` 本地录音
  - `aplay` 本地播放（默认使用 `wav`）
  - 唤醒词别名匹配
  - 最小状态机：`idle -> wake -> record -> think -> speak -> idle`
- 暂未实现：
  - 真正的本地离线唤醒词引擎
  - 多轮对话
  - 天气/音乐真实 API
  - PicoClawAdapter

## 目录

- `src/voice_pet/main.py`：程序入口
- `src/voice_pet/state_machine.py`：状态机
- `src/voice_pet/audio_capture.py`：录音与静音裁剪
- `src/voice_pet/wakeword.py`：唤醒词检测
- `src/voice_pet/asr/mimo_asr.py`：MiMo ASR
- `src/voice_pet/tts/mimo_tts.py`：MiMo TTS
- `src/voice_pet/brain/direct_llm.py`：直接调用 MiMo 文本模型生成回复
- `src/voice_pet/action_router.py`：动作路由占位

## 配置

1. 复制配置：

```bash
cp ~/.picoclaw/voice-pet/config.example.json ~/.picoclaw/voice-pet/config.json
```

2. 设置 API key：

```bash
export MIMO_API_KEY='你的key'
```

也可以直接写到 `config.json` 的 `mimo.api_key`，但更推荐环境变量。

## 运行

```bash
cd ~/.picoclaw/voice-pet
PYTHONPATH=src python3 -m voice_pet.main --config ~/.picoclaw/voice-pet/config.json
```

运行逻辑：

1. 空闲态每次录制一小段环境音
2. 送 MiMo ASR 做文本识别
3. 如果识别结果包含“小爱/小艾/小ai”等别名，则播报“主人，咋啦”
4. 再录制一段用户问题
5. 走动作路由或文本 LLM 回复
6. 用 MiMo TTS 合成 WAV 并本地播放

## 说明

这个版本为了先稳稳跑通，没有引入额外 Python 音频库，而是直接依赖系统命令：

- 录音：`arecord`
- 播放：`aplay`

后续如果要提升体验，再把 idle 态唤醒词替换成真正的流式 VAD / wakeword 引擎即可。
