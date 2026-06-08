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
- 可通过 PicoClaw skills 接入网易云音乐自然语言点歌

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
├── main.py                    # 运行时主入口
├── config.py                  # 配置加载和环境变量覆盖
├── cli/                       # 用户命令行入口
│   ├── ctl.py                 # voice-pet start/status/logs/stop
│   └── config_cli.py          # 模型和音频配置 CLI
├── runtime/                   # 唤醒和会话状态机
│   ├── state_machine.py       # idle/wake/session/think/speak 主流程
│   ├── wakeword.py            # 唤醒词别名和 ASR 容错匹配
│   ├── picoclaw_gateway.py    # 可选 PicoClaw gateway 进程管理
│   └── actions.py             # 可选本地动作路由，默认关闭
├── audio/                     # 本地音频输入输出
│   ├── capture.py             # arecord 录音和静音截断
│   └── player.py              # aplay/BlueALSA 播放
├── mimo/endpoint.py           # MiMo OpenAI-compatible endpoint helper
├── asr/mimo_asr.py            # MiMo ASR 客户端
├── tts/mimo_tts.py            # MiMo TTS 客户端
├── brain/                     # 回复核心适配
│   ├── picoclaw.py            # PicoClaw gateway 桥接适配器
│   └── direct_llm.py          # 调试 fallback，不是默认回复核心
└── tools/                     # 本地调试工具和 mock
    ├── mock_mvp.py
    └── demo_loop.py
voice-pet                      # 用户入口命令
pico_bridge_once.js            # Node WebSocket helper for PicoClaw bridge
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
- 可选网易云音乐点歌：
  - `@music163/ncm-cli`
  - `mpv`
  - PicoClaw 网易云音乐 skills

## 启动流程（从零复现）

下面的流程假设仓库放在 `~/my-project/voice-pet`。如果路径不同，把命令里的路径替换成你的实际路径即可。所有 token 都用占位符表示，不要把真实 token 提交到仓库。

### 1. 准备系统依赖

```bash
cd ~/my-project
git clone https://github.com/dangzitou/voice-pet.git
cd voice-pet

python3 --version
sudo apt update
sudo apt install -y python3 python3-pip alsa-utils nodejs npm

# 可选：蓝牙音箱输出需要 BlueALSA
sudo apt install -y bluez-alsa-utils libasound2-plugin-bluez

pip install -r requirements.txt
npm install
```

`python3 --version` 需要是 Python 3.13 或更新版本。PicoClaw gateway 也需要提前安装或编译好，确保下面至少一个命令能找到：

```bash
which picoclaw
ls ../picoclaw/build/picoclaw
```

### 2. 安装 `voice-pet` 命令

仓库里的 `voice-pet` 脚本会自动设置 `PYTHONPATH`，可以直接使用：

```bash
./voice-pet --help
```

为了任何目录都能执行 `voice-pet start`，建议创建用户级软链接：

```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/voice-pet" ~/.local/bin/voice-pet
export PATH="$HOME/.local/bin:$PATH"
voice-pet --help
```

如果你希望永久生效，把 `export PATH="$HOME/.local/bin:$PATH"` 加到 `~/.bashrc` 或当前 shell 的启动文件里。

### 3. 创建配置和密钥 env 文件

默认配置路径是 `~/.picoclaw/voice-pet/config.json`，默认 env 文件是 `~/.picoclaw/voice-pet/voice-pet.env`。`voice-pet start/status/logs` 会自动加载这个 env 文件。

```bash
mkdir -p ~/.picoclaw/voice-pet ~/.picoclaw/logs
voice-pet config init --config ~/.picoclaw/voice-pet/config.json

cat > ~/.picoclaw/voice-pet/voice-pet.env <<'EOF'
MIMO_API_KEY=<your-mimo-api-key>
PICOCLAW_TOKEN=<your-picoclaw-token>
EOF
chmod 600 ~/.picoclaw/voice-pet/voice-pet.env
```

`MIMO_API_KEY` 是 MiMo 模型调用 key；`PICOCLAW_TOKEN` 是连接 PicoClaw gateway 的 Pico channel token。建议放在 env 文件，不要写进 Git。

### 4. 配置模型、回复核心和音色

```bash
voice-pet config set \
  --base-url https://token-plan-cn.xiaomimimo.com/v1 \
  --model-name mimo-v2.5 \
  --asr-model-name mimo-v2.5-asr \
  --tts-model-name mimo-v2.5-tts \
  --tts-voice 冰糖 \
  --tts-format wav \
  --tts-style-prompt "请用少女感、可爱、年轻一点的中文语气来读，声音自然，像亲近主人的桌宠，语速轻快一点，但不要夸张做作。" \
  --tts-chunk-max-chars 55 \
  --language zh \
  --brain picoclaw \
  --picoclaw-ws-url ws://127.0.0.1:18790/pico/ws \
  --picoclaw-session-id voice-pet \
  --picoclaw-node-script "$(pwd)/pico_bridge_once.js"
```

常用可配置字段：

| 命令参数 | 作用 |
| --- | --- |
| `--base-url` | MiMo OpenAI-compatible endpoint |
| `--model-name` | 文本回复模型名，调试 `direct_llm` 时使用 |
| `--asr-model-name` | MiMo ASR 模型名 |
| `--tts-model-name` | MiMo TTS 模型名 |
| `--tts-voice` | TTS 预置音色，例如 `冰糖`、`茉莉`、`mimo_default` |
| `--tts-style-prompt` | TTS 风格描述 |
| `--tts-chunk-max-chars` | 长回复分段 TTS 的每段最大字数，默认 `55` |
| `--brain picoclaw` | 使用 PicoClaw 作为回复核心 |
| `--picoclaw-ws-url` | PicoClaw gateway WebSocket 地址 |

### 5. 配置 PicoClaw gateway 启动方式

推荐使用 `voice-pet start` 启动完整系统。它会先检查 gateway health；如果 gateway 已经可用就复用，否则尝试启动 `picoclaw gateway -E --host 127.0.0.1`。命令查找顺序是 `PATH` 里的 `picoclaw`，找不到时再尝试仓库同级目录的 `../picoclaw/build/picoclaw`。

如果 gateway 参数需要自定义：

```bash
voice-pet config set \
  --picoclaw-gateway-command picoclaw \
  --picoclaw-gateway-args "gateway" \
  --picoclaw-gateway-ready-url http://127.0.0.1:18790/ready
```

如果 PicoClaw gateway 已经由别的服务管理，启动时使用：

```bash
voice-pet start --no-gateway
```

如果只调试 `python3 -m voice_pet.main` 主循环，而不是 `voice-pet start`，才需要让主循环自己管理 gateway：

```bash
voice-pet config set \
  --manage-picoclaw-gateway \
  --picoclaw-gateway-command picoclaw \
  --picoclaw-gateway-args "gateway" \
  --picoclaw-gateway-ready-url http://127.0.0.1:18790/ready
```

### 6. 配置麦克风和扬声器

先确认系统能看到录音和播放设备：

```bash
arecord -l
aplay -l
aplay -L | sed -n '1,120p'
```

测试麦克风和默认扬声器：

```bash
arecord -D default -f S16_LE -r 16000 -c 1 -d 3 /tmp/voice-pet-mic.wav
aplay /tmp/voice-pet-mic.wav
```

如果麦克风不是默认设备，用 `arecord -l` 里看到的设备配置，例如：

```bash
voice-pet config set \
  --record-device "plughw:CARD=Microphone,DEV=0" \
  --voice-start-threshold 850 \
  --silence-threshold 720 \
  --silence-seconds 0.8 \
  --wake-silence-seconds 1.0 \
  --utterance-silence-seconds 1.2 \
  --wake-max-seconds 0 \
  --utterance-max-seconds 0
```

如果使用普通 ALSA 默认输出：

```bash
voice-pet config set \
  --playback-command aplay \
  --playback-device "" \
  --playback-cooldown 0.5
```

如果使用 BlueALSA 蓝牙音箱，例如 BT501：

```bash
bluetoothctl
# bluetoothctl 里执行：
# scan on
# pair <BT_MAC>
# trust <BT_MAC>
# connect <BT_MAC>
# quit

aplay -L | grep -i bluealsa
voice-pet config set \
  --playback-command aplay \
  --playback-device "bluealsa:DEV=<BT_MAC>,PROFILE=a2dp,VOL=100+" \
  --playback-cooldown 0.5
```

### 7. 预制唤醒确认音频

预制 `主人，咋啦` 可以避免每次唤醒都临时 TTS，响应会更快。

```bash
mkdir -p ~/.picoclaw/voice-pet/runtime
voice-pet demo \
  --text "主人，咋啦" \
  --output ~/.picoclaw/voice-pet/runtime/ack.wav

voice-pet config set \
  --ack-text "主人，咋啦" \
  --ack-audio-path ~/.picoclaw/voice-pet/runtime/ack.wav

aplay ~/.picoclaw/voice-pet/runtime/ack.wav
```

也可以配置多条随机唤醒确认和等待提示：

```bash
voice-pet config set \
  --ack-text-variant "主人，我在。" \
  --ack-text-variant "主人，我在呀。" \
  --thinking-prompt-delay 5 \
  --thinking-prompt-max-delay 20 \
  --thinking-prompt-text "主人，我正在想，马上就好。" \
  --thinking-prompt-text "主人，稍等一下，我还在组织回复。"
```

等待提示会按递增间隔播放：默认第 1 条等待 5 秒，之后分别等待 10、15、20 秒，达到最大间隔后保持 20 秒一条。连接 PicoClaw 时，voice-pet 会优先使用可观察的工具进度播报，例如“我正在查网页资料”“我正在处理音乐播放”；不会播出模型隐藏思维链。

音乐暂停确认也可以配置多条随机话术。第一次触发时会合成到 `~/.picoclaw/voice-pet/runtime/music-control-prompts/`，之后直接播放缓存音频：

```bash
voice-pet config set \
  --music-pause-prompt-text "已经暂停啦，主人。" \
  --music-pause-prompt-text "暂停好啦，主人。"
```

### 8. 可选：网易云自然语言点歌

网易云音乐能力不在 `voice-pet` 里重做 agent。`voice-pet` 只负责把语音请求转给 PicoClaw；搜索、推荐、播放控制由 PicoClaw 的网易云 skills 调用 `ncm-cli` 完成。

安装本地 CLI 和播放器：

```bash
sudo apt install -y mpv
npm install -g @music163/ncm-cli
ncm-cli --version
mpv --version
```

配置网易云开放平台凭证和播放器。凭证只写入本机配置，不要提交到仓库：

```bash
ncm-cli configure

# 或使用非交互命令：
ncm-cli config set appId "<your-netease-app-id>"
ncm-cli config set privateKey "<your-netease-private-key>"
ncm-cli config set player mpv
```

登录网易云账号：

```bash
ncm-cli login
ncm-cli login --check
```

安装 PicoClaw skills。`clawhub` 如果安装到了 OpenClaw workspace，需要复制到 PicoClaw workspace：

```bash
npx clawhub@latest install netease-music-assistant
npx clawhub@latest install netease-music-cli
npx clawhub@latest install ncm-cli-setup

mkdir -p ~/.picoclaw/workspace/skills
for skill in netease-music-assistant netease-music-cli ncm-cli-setup; do
  if [ -d "$HOME/.openclaw/workspace/skills/$skill" ] && [ ! -d "$HOME/.picoclaw/workspace/skills/$skill" ]; then
    cp -a "$HOME/.openclaw/workspace/skills/$skill" "$HOME/.picoclaw/workspace/skills/$skill"
  fi
done

ls ~/.picoclaw/workspace/skills/netease-music-cli/SKILL.md
```

重启 gateway，让 PicoClaw 重新加载 skills：

```bash
voice-pet restart
voice-pet logs --target gateway -n 120
```

日志里应能看到 `Skills: ... available`，并且数量包含新装的网易云 skills。

先直接验证 `ncm-cli`：

```bash
ncm-cli search song --keyword "起风了" --limit 1 --userInput "搜索起风了"
ncm-cli state
ncm-cli stop
```

再走 PicoClaw/voice-pet mock 链路。下面只有唤醒和用户输入是 mock，PicoClaw、网易云播放和 TTS 播放都走真实链路：

```bash
voice-pet mock --wake-text "小爱小爱" --user-text "小爱播放邓紫棋的你把我灌醉" --play
ncm-cli state
```

点歌类请求会先由 voice-pet 口播 PicoClaw 的提示，例如“正在播放……”，提示音播完后再释放 `ncm-cli` 启动 `mpv` 正式播放音乐。这样可以避免提示音和音乐同时抢同一个蓝牙/ALSA 输出。

音乐播放期间 voice-pet 仍会保持麦克风监听，但会进入音乐控制模式：只响应带 `小爱` 前缀的播放控制，例如 `小爱暂停播放`、`小爱停止播放`、`小爱结束播放`、`小爱继续播放`。其他内容，包括 `小爱阿爸阿爸` 这类非控制语音，会记录为忽略，不会转发给 PicoClaw，也不会打断音乐。新的点歌请求，例如 `小爱播放一首周杰伦的歌`，会转给 PicoClaw 处理。暂停成功后会随机播放一条本地缓存提示音，例如“已经暂停啦，主人。”；提示话术可在 `music.pause_prompt_texts` 里配置，默认 10 条。暂停后可以继续对话，`小爱继续播放` 会恢复音乐。

如果 `ncm-cli play` 显示已经解析到歌曲，但提示 `daemon 无响应` 或没有 `mpv` 进程，这是 `ncm-cli` 本地播放器 daemon 问题，不是 `voice-pet` 的 ASR/TTS 问题。优先检查：

```bash
tail -n 120 ~/.config/ncm-cli/app.log
pgrep -a mpv
ls ~/.picoclaw/voice-pet/runtime/external-audio-*
ncm-cli stop
```

如果本机设置了代理，网易云音频 CDN 可能走代理失败。测试时可以清掉代理环境：

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
  ncm-cli search song --keyword "起风了" --limit 1 --userInput "搜索起风了"
```

蓝牙音箱输出建议让 `mpv` 固定到 BlueALSA 设备，例如：

```bash
mkdir -p ~/.config/mpv
cat > ~/.config/mpv/mpv.conf <<'EOF'
ao=alsa
audio-device=alsa/bluealsa:DEV=<BT_MAC>,PROFILE=a2dp,VOL=100+
EOF
```

### 9. 跑启动前验证

不依赖 MiMo/PicoClaw 的离线闭环：

```bash
voice-pet mock --offline --wake-text "小爱小爱" --user-text "小爱今天厦门天气咋样"
```

依赖真实 MiMo/PicoClaw 的闭环：

```bash
voice-pet mock --wake-text "小爱小爱" --user-text "小爱你好，请只回复：ok"
```

查看最终配置。`show` 只会显示 key 是否已设置，不会打印真实 key：

```bash
voice-pet config show
```

### 10. 启动完整系统

`voice-pet start` 会在后台启动 PicoClaw gateway 和 voice-pet runtime；如果 gateway 已经健康运行，它会复用已有 gateway。启动完成后会自动实时输出 voice-pet 日志，按 `Ctrl+C` 只会退出日志跟随，后台服务会继续运行。启动时会加载 `~/.picoclaw/voice-pet/voice-pet.env`，并清掉本机代理环境，避免 localhost gateway 请求被代理干扰。

```bash
voice-pet start
voice-pet status
```

如果只想后台静默启动，不跟随日志：

```bash
voice-pet start --detach
```

如果你已经用别的方式启动 PicoClaw gateway，只启动语音 runtime：

```bash
voice-pet start --no-gateway
```

常用命令：

| 命令 | 用途 |
| --- | --- |
| `voice-pet start` | 启动 PicoClaw gateway 和 voice-pet，并实时输出 voice-pet 日志 |
| `voice-pet start --detach` | 后台启动完整系统，不跟随日志 |
| `voice-pet start --no-gateway` | 只启动 voice-pet，连接已有 gateway，并实时输出日志 |
| `voice-pet start --no-gateway --detach` | 只后台启动 voice-pet，不跟随日志 |
| `voice-pet stop` | 停止 voice-pet 和 PicoClaw gateway |
| `voice-pet stop --no-gateway` | 只停止 voice-pet |
| `voice-pet restart` | 重启完整语音 runtime |
| `voice-pet status` | 查看 gateway/voice-pet 进程和健康状态 |
| `voice-pet logs -f` | 实时查看 voice-pet 日志 |
| `voice-pet logs --target gateway -f` | 实时查看 PicoClaw gateway 日志 |
| `voice-pet mic-test` | 实时显示一行麦克风音量条；超过阈值后收完整段音频并输出 MiMo ASR 结果；会自动临时暂停/恢复 voice-pet |
| `voice-pet mic-test --no-asr` | 只看麦克风音量条，不调用 ASR |
| `voice-pet mic-test --list-devices` | 列出 ALSA 录音设备 |
| `voice-pet config show` | 查看当前模型、音频和 runtime 配置 |
| `voice-pet config set --tts-chunk-max-chars 45` | 调整长回复分段 TTS 的每段字数；越小越快听到第一段 |
| `voice-pet config set --playback-cooldown 0.5` | 设置每次播放后再开麦前的等待时间 |
| `voice-pet config set --thinking-prompt-delay 5 --thinking-prompt-max-delay 20` | 设置回复等待提示递增间隔：5、10、15、20、20 秒 |
| `voice-pet config set --music-pause-prompt-text "已经暂停啦，主人。"` | 追加一条音乐暂停确认随机话术 |
| `voice-pet config set --wake-silence-seconds 1.0 --utterance-silence-seconds 1.2 --wake-max-seconds 0 --utterance-max-seconds 0` | 持续收完整段人声，`max=0` 表示不按固定时长切段 |
| `voice-pet demo --text "主人，咋啦"` | 跑一次 MiMo TTS/ASR 调试 demo |
| `voice-pet mock --offline --wake-text "小爱小爱" --user-text "小爱今天厦门天气咋样"` | 跑离线 mock 闭环测试 |
| `voice-pet mock --wake-text "小爱小爱" --user-text "小爱播放邓紫棋的你把我灌醉" --play` | 用 mock 输入跑真实 PicoClaw/网易云/TTS 播放链路 |
| `ncm-cli login --check` | 检查网易云 CLI 登录态 |
| `ncm-cli search song --keyword "起风了" --limit 1 --userInput "搜索起风了"` | 验证网易云搜索能力 |
| `ncm-cli state` | 查看网易云本地播放器状态 |
| `ncm-cli stop` | 停止网易云本地播放 |

日志默认写入：

```text
~/.picoclaw/voice-pet/runtime/voice-pet.log
~/.picoclaw/logs/gateway-voice-pet.log
```

### 11. 真实语音测试

启动后按这个顺序测试：

1. 说 `小爱小爱`
2. 听到预制音频 `主人，咋啦`
3. 继续说 `小爱今天厦门天气咋样`
4. 等 PicoClaw 返回后，voice-pet 会用 MiMo TTS 合成并通过扬声器播放

唤醒态里的后续对话也必须以 `小爱` 开头。不带前缀的语音会记录为 `ignored non-prefixed speech=...`，不会转发给 PicoClaw。60 秒没有可处理语音会退出唤醒态。

如果当前正在播放音乐，后续语音不会进入普通问答，只会尝试匹配音乐控制指令。可用示例：

```text
小爱暂停播放
小爱停止播放
小爱结束播放
小爱继续播放
```

`小爱暂停播放` 成功后会播放随机暂停确认音；`小爱阿爸阿爸` 这类非控制内容只会被忽略。

### 12. 常见排障

| 现象 | 检查 |
| --- | --- |
| `voice-pet` 命令找不到 | 确认 `~/.local/bin` 在 `PATH`，或在仓库内使用 `./voice-pet` |
| gateway 不健康 | 跑 `voice-pet logs --target gateway -f`，确认 `picoclaw gateway -E --host 127.0.0.1` 能启动 |
| `PICOCLAW_TOKEN` 或 `MIMO_API_KEY` 未设置 | 检查 `~/.picoclaw/voice-pet/voice-pet.env`，再跑 `voice-pet config show` |
| 没有录到声音 | 跑 `arecord -l` 和 `arecord -D <device> ...`，再调整 `--record-device` |
| 误触发或不触发 | 调整 `--voice-start-threshold`、`--silence-threshold`、`--silence-seconds` |
| 没有播放声音 | 跑 `aplay -l`、`aplay -L`，确认 `--playback-device` 能被 `aplay -D` 使用 |
| 蓝牙连接失败 | 先用 `bluetoothctl connect <BT_MAC>` 和 `aplay -L | grep -i bluealsa` 确认系统层可用 |
| 回复太长或太慢 | 调小 `--tts-chunk-max-chars`，并保持 PicoClaw 回复 prompt 简短；长回复会完整分段播完 |
| 点歌没反应 | 跑 `voice-pet logs --target gateway -f`，确认 PicoClaw 已加载 `netease-music-cli` 和 `netease-music-assistant` |
| 搜到歌但不播放 | 跑 `ncm-cli state`、`pgrep -a mpv`、`tail -n 120 ~/.config/ncm-cli/app.log`，并检查 `~/.picoclaw/voice-pet/runtime/external-audio-*` 是否有未释放的延迟播放信号 |
| 网易云请求 502 或音频 URL 无法访问 | 清掉代理环境后重试，或确认 `voice-pet start` 启动的 gateway 环境里没有 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY` |

### 13. 可选：systemd 用户服务

仓库里有 `voice-pet.service` 示例。它默认假设代码安装在 `~/.picoclaw/voice-pet`；如果你的仓库在别的路径，先修改 service 里的 `WorkingDirectory` 和 `PYTHONPATH`。

```bash
mkdir -p ~/.config/systemd/user
cp voice-pet.service ~/.config/systemd/user/voice-pet.service
systemctl --user daemon-reload
systemctl --user enable voice-pet
systemctl --user start voice-pet
systemctl --user status voice-pet
```

调试 systemd 日志：

```bash
journalctl --user -u voice-pet -f
```

## 运行流程

1. 本地持续读取麦克风 PCM
2. 长期开启麦克风原始 PCM 流，超过人声阈值后开始缓存，直到持续静音才结束这一整段语音
3. 使用 MiMo ASR 转写候选音频
4. 匹配唤醒词别名
5. 如果匹配“小爱”，立即播报 `主人，咋啦`
6. 进入唤醒态，继续流式等待用户语音
7. 用户语音经 MiMo ASR 转文字；只有以 `小爱` 开头的内容才转发给 PicoClaw gateway
8. PicoClaw 返回回复文本
9. 使用 MiMo TTS 合成回复并本地播放
10. 播放完成并经过 `playback_cooldown_seconds` 后，才继续等待下一段带前缀的用户语音；60 秒没有可处理语音则退出唤醒态

## 说明

当前 MVP 直接依赖系统音频命令，而不是额外引入 Python 音频栈。这样可以减少运行时依赖，便于在树莓派上部署和排查问题。

当前“流式唤醒”是轻量的本地语音活动门控，唤醒词本身仍由 MiMo ASR 转写后匹配。`voice-pet` 不重做 PicoClaw agent；agent、memory、tools 继续由 PicoClaw 负责。
