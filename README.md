# voice-pet

`voice-pet` 是一个面向树莓派的外置语音和桌宠 runtime。

它运行在 PicoClaw 外侧，负责麦克风采集、流式人声检测、唤醒词确认、MiMo ASR/TTS 和本地播报；PicoClaw 作为回复核心，负责 agent、memory 和 tools。`voice-pet` 可以连接已有 PicoClaw gateway，也可以按配置启动 gateway。

English README: [README.en.md](./README.en.md)

## 项目特性

- 集成 MiMo ASR 和 TTS
- 使用 `arecord` 进行本地录音
- 使用本地流式语音活动检测减少空闲态 ASR 请求
- 使用 `aplay` 进行本地播放
- 支持唤醒词别名匹配：`小爱`、`小艾`、`小ai`、`xiao ai`、`xiaoai`
- 唤醒后立即播报 `主人，咋啦`
- 唤醒态内连续语音转写并转发给 PicoClaw
- 60 秒没有新的用户语音时自动退出唤醒态
- 可选启动和管理 PicoClaw gateway 进程

## 当前状态

当前 MVP 已实现：

- 端到端闭环：监听 -> 转写 -> 回复 -> 合成 -> 播放
- 最小运行时状态机：`idle -> wake -> session -> think -> speak -> session/idle`
- 本地流式语音活动检测：持续读取麦克风 PCM，检测到完整语音片段后才调用 ASR
- MiMo 的 ASR/TTS 适配器
- PicoClaw gateway 桥接适配器

暂未实现：

- 离线关键词唤醒引擎
- 打断处理
- 桌宠动作/表情 runtime

## 项目结构

```text
src/voice_pet/
├── main.py                # CLI 入口
├── config_cli.py          # 配置管理 CLI
├── state_machine.py       # 运行时主循环
├── picoclaw_gateway.py    # 可选 PicoClaw gateway 进程管理
├── audio_capture.py       # 录音与静音截断
├── wakeword.py            # 唤醒词别名匹配
├── action_router.py       # 可选本地动作路由，默认关闭
├── mock_mvp.py            # 用 TTS mock 输入的闭环测试
├── asr/mimo_asr.py        # MiMo ASR 客户端
├── tts/mimo_tts.py        # MiMo TTS 客户端
├── brain/picoclaw.py      # PicoClaw gateway 桥接适配器
└── brain/direct_llm.py    # 调试 fallback，不是默认回复核心
pico_bridge_once.js        # Node WebSocket helper for PicoClaw bridge
```

## 环境要求

- Python 3.13+
- ALSA 工具：
  - `arecord`
  - `aplay`
- 可选蓝牙输出：
  - `bluez-alsa-utils`
  - `libasound2-plugin-bluez`
- MiMo API key
- Node.js 和 npm 依赖：
  - `npm install`
- PicoClaw gateway 和 Pico channel token

## 配置

复制示例配置：

```bash
cp ~/.picoclaw/voice-pet/config.example.json ~/.picoclaw/voice-pet/config.json
```

通过环境变量设置 API key：

```bash
export MIMO_API_KEY="<your-token>"
export PICOCLAW_TOKEN="<your-pico-token>"
```

也可以直接写入 `config.json`。如果同时配置了环境变量，环境变量会覆盖 `config.json` 里的值。

也可以用配置 CLI 创建、查看和修改语音模型配置：

```bash
PYTHONPATH=src python3 -m voice_pet.config_cli init --config ./config.json
PYTHONPATH=src python3 -m voice_pet.config_cli show --config ./config.json
PYTHONPATH=src python3 -m voice_pet.config_cli set --config ./config.json \
  --base-url https://token-plan-cn.xiaomimimo.com/v1 \
  --api-key "<your-mimo-token>" \
  --model-name mimo-v2.5 \
  --asr-model-name mimo-v2.5-asr \
  --tts-model-name mimo-v2.5-tts \
  --tts-voice mimo_default \
  --language zh \
  --playback-command aplay \
  --brain picoclaw \
  --picoclaw-ws-url ws://127.0.0.1:18790/pico/ws \
  --picoclaw-session-id voice-pet
```

支持修改的语音模型字段包括：`--api-key`、`--clear-api-key`、`--api-base` / `--base-url`、`--model` / `--llm-model` / `--model-name`、`--asr-model` / `--asr-model-name`、`--tts-model` / `--tts-model-name`、`--tts-voice`、`--tts-format`、`--language`。音频播放可以用 `--playback-command` 和 `--playback-device` 配置；`show` 只会显示 API key 是否已配置，不会把 key 打印出来。

如果要固定输出到 BlueALSA 蓝牙音箱，例如 BT501：

```bash
PYTHONPATH=src python3 -m voice_pet.config_cli set --config ./config.json \
  --playback-command aplay \
  --playback-device "bluealsa:DEV=D6:BF:DF:4A:EF:E2,PROFILE=a2dp,VOL=100+"
```

如果要把“主人，咋啦”预制成音频，先用 TTS demo 生成一次，再把路径写入配置：

```bash
PYTHONPATH=src python3 -m voice_pet.demo_loop --config ./config.json \
  --text "主人，咋啦" \
  --output ~/.picoclaw/voice-pet/runtime/ack.wav
PYTHONPATH=src python3 -m voice_pet.config_cli set --config ./config.json \
  --ack-text "主人，咋啦" \
  --ack-audio-path ~/.picoclaw/voice-pet/runtime/ack.wav
```

配置了 `wakeword.ack_audio_path` 后，唤醒确认会直接播放这个音频文件；文件不存在时回退到 `wakeword.ack_text` 的 TTS 合成。

如果希望由 `voice-pet` 启动 PicoClaw gateway：

```bash
PYTHONPATH=src python3 -m voice_pet.config_cli set --config ./config.json \
  --manage-picoclaw-gateway \
  --picoclaw-gateway-command picoclaw \
  --picoclaw-gateway-args "gateway" \
  --picoclaw-gateway-ready-url http://127.0.0.1:18790/ready
```

默认是连接已经运行的 gateway。PicoClaw token 不通过 CLI 写入配置，继续用 `PICOCLAW_TOKEN` 环境变量。

默认使用本地流式监听：

- `audio.listen_mode = "streaming"`：持续读取麦克风音频，只在检测到一段语音后调用 ASR 确认唤醒词
- `audio.listen_mode = "fixed_window"`：切回旧的固定窗口录音模式，便于排查阈值或麦克风问题
- `audio.voice_start_threshold` / `audio.silence_threshold`：控制语音开始和静音结束阈值
- `audio.stream_chunk_ms` / `audio.pre_roll_seconds`：控制流式读取块大小和触发前保留音频
- `wakeword.session_timeout_seconds = 60.0`：唤醒后 60 秒没有新语音就退出唤醒态
- `wakeword.ack_audio_path`：预制唤醒确认音频路径，配置后优先直接播放

## 运行

安装依赖：

```bash
pip install -r requirements.txt
npm install
```

启动完整系统：

```bash
cd /home/zitou/my-project/voice-pet
./voice-petctl start
```

查看状态、日志和停止：

```bash
./voice-petctl status
./voice-petctl logs -f
./voice-petctl stop
```

`start` 会在后台启动 PicoClaw gateway 和 voice-pet，自动加载 `~/.picoclaw/voice-pet/voice-pet.env`，并清掉本机代理环境，避免 localhost gateway 被代理干扰。日志默认写入：

```text
~/.picoclaw/voice-pet/runtime/voice-pet.log
~/.picoclaw/logs/gateway-voice-pet.log
```

调试时也可以只启动主循环：

```bash
PYTHONPATH=src python3 -m voice_pet.main --config ~/.picoclaw/voice-pet/config.json
```

运行 TTS / ASR 演示：

```bash
cd ~/.picoclaw/voice-pet
PYTHONPATH=src python3 -m voice_pet.demo_loop --config ~/.picoclaw/voice-pet/config.json --text "主人，咋啦"
```

运行 mock 闭环测试：

```bash
cd ~/.picoclaw/voice-pet
PYTHONPATH=src python3 -m voice_pet.mock_mvp --config ~/.picoclaw/voice-pet/config.json --wake-text "小爱小爱" --user-text "你好，请只回复：ok"
```

不依赖 MiMo/PicoClaw 的离线 mock：

```bash
PYTHONPATH=src python3 -m voice_pet.mock_mvp --offline --wake-text "小爱小爱" --user-text "今天天气怎么样"
```

## 运行流程

1. 本地持续读取麦克风 PCM
2. 检测到一段完整语音后写入候选唤醒音频
3. 使用 MiMo ASR 转写候选音频
4. 匹配唤醒词别名
5. 如果匹配“小爱”，立即播报 `主人，咋啦`
6. 进入唤醒态，继续流式等待用户语音
7. 用户语音经 MiMo ASR 转文字后转发给 PicoClaw gateway
8. PicoClaw 返回回复文本
9. 使用 MiMo TTS 合成回复并本地播放
10. 播放后继续等待下一段用户语音；60 秒无新语音则退出唤醒态

## 说明

当前 MVP 直接依赖系统音频命令，而不是额外引入 Python 音频栈。这样可以减少运行时依赖，便于在树莓派上部署和排查问题。

当前“流式唤醒”是轻量的本地语音活动门控，唤醒词本身仍由 MiMo ASR 转写后匹配。`voice-pet` 不重做 PicoClaw agent；agent、memory、tools 继续由 PicoClaw 负责。
