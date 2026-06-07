# voice-pet

`voice-pet` 是一个面向树莓派的本地语音助手原型。

它运行在 PicoClaw 之外，负责麦克风采集、唤醒词检测、语音转写、回复生成和本地播报；后续可以通过适配层接入 PicoClaw 的 agent、memory 和 tools。

English README: [README.en.md](./README.en.md)

## 项目特性

- 集成 MiMo ASR、TTS 和文本模型
- 使用 `arecord` 进行本地录音
- 使用 `aplay` 进行本地播放
- 支持唤醒词别名匹配：`小爱`、`小艾`、`小ai`、`xiao ai`、`xiaoai`
- 提供单轮语音交互状态机
- 预留天气、音乐、设备控制等动作路由接口
- 可切换为 PicoClaw gateway 作为回复后端

## 当前状态

当前 MVP 已实现：

- 端到端闭环：监听 -> 转写 -> 回复 -> 合成 -> 播放
- 最小运行时状态机：`idle -> wake -> record -> think -> speak -> idle`
- MiMo 的 ASR、TTS 和文本回复适配器

暂未实现：

- 流式 VAD / 离线唤醒词引擎
- 多轮会话管理
- 真实天气 / 音乐服务集成
- PicoClaw 适配器

## 项目结构

```text
src/voice_pet/
├── main.py                # CLI 入口
├── state_machine.py       # 运行时主循环
├── audio_capture.py       # 录音与静音截断
├── wakeword.py            # 唤醒词别名匹配
├── action_router.py       # 动作路由扩展点
├── mock_mvp.py            # 用 TTS mock 输入的闭环测试
├── asr/mimo_asr.py        # MiMo ASR 客户端
├── tts/mimo_tts.py        # MiMo TTS 客户端
├── brain/direct_llm.py    # 直接调用 MiMo 文本模型
└── brain/picoclaw.py      # PicoClaw gateway 桥接适配器
pico_bridge_once.js        # Node WebSocket helper for PicoClaw bridge
```

## 环境要求

- Python 3.13+
- ALSA 工具：
  - `arecord`
  - `aplay`
- MiMo API key

## 配置

复制示例配置：

```bash
cp ~/.picoclaw/voice-pet/config.example.json ~/.picoclaw/voice-pet/config.json
```

通过环境变量设置 API key：

```bash
export MIMO_API_KEY="<your-token>"
```

也可以直接写入 `config.json`，但更推荐使用环境变量。

如果要把 PicoClaw 作为回复后端，还需要在 `config.json` 里设置：

- `runtime.brain = "picoclaw"`
- `runtime.picoclaw_ws_url`
- `runtime.picoclaw_token`
- `runtime.picoclaw_session_id`

## 运行

启动主循环：

```bash
cd ~/.picoclaw/voice-pet
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

## 运行流程

1. 录制一段空闲态环境音
2. 使用 MiMo ASR 转写音频
3. 匹配唤醒词别名
4. 播报 `主人，咋啦`
5. 录制用户问题
6. 路由到动作处理器或文本模型
7. 使用 MiMo TTS 合成回复
8. 本地播放结果音频

## 说明

当前 MVP 直接依赖系统音频命令，而不是额外引入 Python 音频栈。这样可以减少运行时依赖，便于在树莓派上部署和排查问题。

下一步重点会放在流式唤醒、打断处理和动作集成上。
