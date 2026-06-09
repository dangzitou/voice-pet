# voice-pet

`voice-pet` is an external voice and desktop-pet runtime for Raspberry Pi.

It runs outside PicoClaw and handles microphone capture, streaming voice activity detection, wakeword confirmation, MiMo ASR/TTS, and local playback. PicoClaw remains the reply core for agents, memory, and tools. `voice-pet` can connect to an existing PicoClaw gateway or start one from its own runtime configuration.

中文说明： [README.md](./README.md)

## Features

- MiMo ASR and TTS integration
- Local audio capture via `arecord`
- Local streaming voice activity detection to reduce idle ASR requests
- Local playback via `aplay`
- Wakeword alias matching: `小爱`, `小艾`, `小ai`, `xiao ai`, `xiaoai`
- Immediate wake acknowledgement with `主人，咋啦`
- Wake-session speech transcription and forwarding to PicoClaw
- Automatic wake-session timeout after 60 seconds without user speech
- Optional PicoClaw gateway process management

## Status

Implemented in the current MVP:

- end-to-end loop: listen -> transcribe -> reply -> synthesize -> play
- minimal runtime state machine: `idle -> wake -> session -> think -> speak -> session/idle`
- local streaming voice activity detection: continuously reads microphone PCM and calls ASR only after a complete speech segment is captured
- MiMo adapters for ASR/TTS
- PicoClaw gateway bridge adapter

Not implemented yet:

- offline keyword wakeword engine
- interruption handling
- desktop-pet action/expression runtime

## Project Layout

```text
src/voice_pet/
├── main.py                    # runtime entrypoint
├── config.py                  # config loading and env overrides
├── cli/                       # user-facing command line entrypoints
│   ├── ctl.py                 # voice-pet start/status/logs/stop
│   └── config_cli.py          # model and audio configuration CLI
├── runtime/                   # wake/session state machine
│   ├── state_machine.py       # idle/wake/session/think/speak flow
│   ├── wakeword.py            # wakeword aliases and ASR-tolerant matching
│   ├── picoclaw_gateway.py    # optional PicoClaw gateway process management
│   └── actions.py             # optional local action routing, disabled by default
├── audio/                     # local audio input/output
│   ├── capture.py             # arecord capture and silence cut-off
│   └── player.py              # aplay/BlueALSA playback
├── mimo/endpoint.py           # MiMo OpenAI-compatible endpoint helper
├── asr/mimo_asr.py            # MiMo ASR client
├── tts/mimo_tts.py            # MiMo TTS client
├── brain/                     # reply core adapters
│   ├── picoclaw.py            # PicoClaw gateway bridge adapter
│   └── direct_llm.py          # debugging fallback, not the default reply core
└── tools/                     # local debug tools and mocks
    ├── mock_mvp.py
    └── demo_loop.py
voice-pet                      # user command
pico_bridge_session.js         # Node persistent WebSocket helper for PicoClaw bridge
```

## Requirements

- Python 3.13+
- ALSA tools:
  - `arecord`
  - `aplay`
- Optional Bluetooth output:
  - `bluez-alsa-utils`
  - `libasound2-plugin-bluez`
- MiMo API key
- Node.js and npm dependencies:
  - `npm install`
- PicoClaw gateway and Pico channel token

## Startup Runbook

The commands below assume the repository is at `~/my-project/voice-pet`. If you use a different path, replace that path in the commands. Tokens are shown as placeholders; do not commit real tokens.

### 1. Install System Dependencies

```bash
cd ~/my-project
git clone https://github.com/dangzitou/voice-pet.git
cd voice-pet

python3 --version
sudo apt update
sudo apt install -y python3 python3-pip alsa-utils nodejs npm

# Optional: BlueALSA is needed for Bluetooth speaker output.
sudo apt install -y bluez-alsa-utils libasound2-plugin-bluez

pip install -r requirements.txt
npm install
```

`python3 --version` should be Python 3.13 or newer. PicoClaw gateway must also be installed or built; at least one of these should work:

```bash
which picoclaw
ls ../picoclaw/build/picoclaw
```

### 2. Install the `voice-pet` Command

The repository's `voice-pet` script sets `PYTHONPATH` automatically and can be run directly:

```bash
./voice-pet --help
```

To run `voice-pet start` from any directory, create a user-level symlink:

```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/voice-pet" ~/.local/bin/voice-pet
export PATH="$HOME/.local/bin:$PATH"
voice-pet --help
```

To make this permanent, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` or your shell startup file.

### 3. Create Config and Env Files

The default config path is `~/.picoclaw/voice-pet/config.json`; the default env file is `~/.picoclaw/voice-pet/voice-pet.env`. `voice-pet start/status/logs` load this env file automatically.

```bash
mkdir -p ~/.picoclaw/voice-pet ~/.picoclaw/logs
voice-pet config init --config ~/.picoclaw/voice-pet/config.json

cat > ~/.picoclaw/voice-pet/voice-pet.env <<'EOF'
MIMO_API_KEY=<your-mimo-api-key>
PICOCLAW_TOKEN=<your-picoclaw-token>
EOF
chmod 600 ~/.picoclaw/voice-pet/voice-pet.env
```

`MIMO_API_KEY` is the MiMo model key. `PICOCLAW_TOKEN` is the Pico channel token used to connect to PicoClaw gateway. Keep them in the env file instead of Git.

### 4. Configure Models, Reply Core, and Voice

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
  --picoclaw-node-script "$(pwd)/pico_bridge_session.js"
```

Common fields:

| Option | Purpose |
| --- | --- |
| `--base-url` | MiMo OpenAI-compatible endpoint |
| `--model-name` | text reply model, used by the `direct_llm` debug backend |
| `--asr-model-name` | MiMo ASR model |
| `--tts-model-name` | MiMo TTS model |
| `--tts-voice` | preset TTS voice, for example `冰糖`, `茉莉`, or `mimo_default` |
| `--tts-style-prompt` | TTS style instruction |
| `--tts-chunk-max-chars` | maximum characters per TTS chunk for long replies, default `55` |
| `--brain picoclaw` | use PicoClaw as the reply core |
| `--picoclaw-ws-url` | PicoClaw gateway WebSocket URL |

### 5. Configure PicoClaw Gateway Startup

The recommended path is `voice-pet start`. It checks gateway health first; if the gateway is already available, it reuses it. Otherwise, it tries to start `picoclaw gateway -E --host 127.0.0.1`. Command resolution checks `picoclaw` in `PATH` first, then falls back to `../picoclaw/build/picoclaw` next to this repository.

If you need custom gateway arguments:

```bash
voice-pet config set \
  --picoclaw-gateway-command picoclaw \
  --picoclaw-gateway-args "gateway" \
  --picoclaw-gateway-ready-url http://127.0.0.1:18790/ready
```

If PicoClaw gateway is managed by another service, start with:

```bash
voice-pet start --no-gateway
```

If you debug only the `python3 -m voice_pet.main` loop instead of `voice-pet start`, enable gateway management inside the main loop:

```bash
voice-pet config set \
  --manage-picoclaw-gateway \
  --picoclaw-gateway-command picoclaw \
  --picoclaw-gateway-args "gateway" \
  --picoclaw-gateway-ready-url http://127.0.0.1:18790/ready
```

### 6. Configure Microphone and Speaker

Confirm the system can see capture and playback devices:

```bash
arecord -l
aplay -l
aplay -L | sed -n '1,120p'
```

Test the microphone and default speaker:

```bash
arecord -D default -f S16_LE -r 16000 -c 1 -d 3 /tmp/voice-pet-mic.wav
aplay /tmp/voice-pet-mic.wav
```

If the microphone is not the default device, use the device shown by `arecord -l`, for example:

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

For normal ALSA default output:

```bash
voice-pet config set \
  --playback-command aplay \
  --playback-device "" \
  --playback-cooldown 0.5
```

For a BlueALSA Bluetooth speaker such as BT501:

```bash
bluetoothctl
# Inside bluetoothctl:
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

### 7. Prebuild the Wake Acknowledgement

Prebuilding `主人，咋啦` avoids doing TTS at wake time and makes wake acknowledgement faster.

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

Optional random wake acknowledgements and thinking prompts:

```bash
voice-pet config set \
  --ack-text-variant "主人，我在。" \
  --ack-text-variant "主人，我在呀。" \
  --thinking-prompt-delay 3 \
  --thinking-prompt-text "主人，我正在想，马上就好。" \
  --thinking-prompt-text "主人，稍等一下，我还在组织回复。"
```

### 8. Run Preflight Checks

Offline loop without MiMo/PicoClaw:

```bash
voice-pet mock --offline --wake-text "小爱小爱" --user-text "小爱今天厦门天气咋样"
```

Real MiMo/PicoClaw loop:

```bash
voice-pet mock --wake-text "小爱小爱" --user-text "小爱你好，请只回复：ok"
```

Show final config. `show` only prints whether keys are set; it never prints real keys:

```bash
voice-pet config show
```

### 9. Start the Full System

`voice-pet start` launches PicoClaw gateway and the voice-pet runtime in the background. If the gateway is already healthy, it reuses it. After startup it follows the voice-pet log in real time; pressing `Ctrl+C` only stops log following, while the background services keep running. Startup loads `~/.picoclaw/voice-pet/voice-pet.env` and strips local proxy env vars so localhost gateway traffic is not routed through a proxy.

```bash
voice-pet start
voice-pet status
```

To start quietly in the background without following logs:

```bash
voice-pet start --detach
```

If PicoClaw gateway is already managed elsewhere, start only the voice runtime:

```bash
voice-pet start --no-gateway
```

Common commands:

| Command | Purpose |
| --- | --- |
| `voice-pet start` | Start PicoClaw gateway and voice-pet, then follow voice-pet logs |
| `voice-pet start --detach` | Start the full system in the background without following logs |
| `voice-pet start --no-gateway` | Start only voice-pet, connect to an existing gateway, then follow logs |
| `voice-pet start --no-gateway --detach` | Start only voice-pet in the background without following logs |
| `voice-pet stop` | Stop voice-pet and PicoClaw gateway |
| `voice-pet stop --no-gateway` | Stop only voice-pet |
| `voice-pet restart` | Restart the full voice runtime |
| `voice-pet status` | Show gateway/voice-pet process and health status |
| `voice-pet logs -f` | Follow voice-pet logs |
| `voice-pet logs --target gateway -f` | Follow PicoClaw gateway logs |
| `voice-pet config show` | Show current model, audio, and runtime configuration |
| `voice-pet config set --playback-cooldown 0.5` | Set the wait time after playback before recording again |
| `voice-pet config set --wake-silence-seconds 1.0 --utterance-silence-seconds 1.2 --wake-max-seconds 0 --utterance-max-seconds 0` | Capture each full speech segment continuously; `max=0` disables fixed-duration cuts |
| `voice-pet demo --text "主人，咋啦"` | Run a MiMo TTS/ASR debug demo |
| `voice-pet mock --offline --wake-text "小爱小爱" --user-text "小爱今天厦门天气咋样"` | Run the offline mock end-to-end test |

Default logs:

```text
~/.picoclaw/voice-pet/runtime/voice-pet.log
~/.picoclaw/logs/gateway-voice-pet.log
```

### 10. Test With Real Speech

After startup, test in this order:

1. Say `小爱小爱`
2. You should hear the prebuilt `主人，咋啦`
3. Say `小爱今天厦门天气咋样`
4. After PicoClaw replies, voice-pet synthesizes the answer with MiMo TTS and plays it through the speaker

Follow-up speech inside wake mode must also start with `小爱`. Speech without the prefix is logged as `ignored non-prefixed speech=...` and is not forwarded to PicoClaw. Wake mode exits after 60 seconds without processable speech.

### 11. Troubleshooting

| Symptom | Check |
| --- | --- |
| `voice-pet` command not found | Ensure `~/.local/bin` is in `PATH`, or run `./voice-pet` inside the repo |
| gateway is unhealthy | Run `voice-pet logs --target gateway -f`; verify `picoclaw gateway -E --host 127.0.0.1` starts |
| `PICOCLAW_TOKEN` or `MIMO_API_KEY` is missing | Check `~/.picoclaw/voice-pet/voice-pet.env`, then run `voice-pet config show` |
| microphone captures nothing | Run `arecord -l` and `arecord -D <device> ...`, then set `--record-device` |
| false triggers or missed speech | Tune `--voice-start-threshold`, `--silence-threshold`, and `--silence-seconds` |
| no playback | Run `aplay -l` and `aplay -L`; ensure `--playback-device` works with `aplay -D` |
| Bluetooth cannot connect | First verify `bluetoothctl connect <BT_MAC>` and `aplay -L | grep -i bluealsa` at the system level |
| replies are too long or slow | Lower `--tts-chunk-max-chars` and keep the PicoClaw reply prompt concise; long replies are still spoken in full |

### 12. Optional: systemd User Service

The repository includes `voice-pet.service`. It assumes the code is installed at `~/.picoclaw/voice-pet`; if your repo is elsewhere, edit `WorkingDirectory` and `PYTHONPATH` first.

```bash
mkdir -p ~/.config/systemd/user
cp voice-pet.service ~/.config/systemd/user/voice-pet.service
systemctl --user daemon-reload
systemctl --user enable voice-pet
systemctl --user start voice-pet
systemctl --user status voice-pet
```

Debug systemd logs:

```bash
journalctl --user -u voice-pet -f
```

## Runtime Flow

1. Continuously read microphone PCM locally
2. Keep one raw microphone PCM stream open; after audio exceeds the speech threshold, buffer continuously until sustained silence ends the full speech segment
3. Transcribe the candidate audio with MiMo ASR
4. Match wakeword aliases
5. If the wakeword matches, immediately acknowledge with `主人，咋啦`
6. Enter wake mode and continue waiting for user speech
7. Transcribe user speech with MiMo ASR; only speech prefixed with `小爱` is forwarded to PicoClaw gateway
8. Receive the reply text from PicoClaw
9. Synthesize the reply with MiMo TTS and play it locally
10. Wait until playback finishes and `playback_cooldown_seconds` elapses, then continue waiting for the next prefixed user utterance; leave wake mode after 60 seconds without processable speech

## Notes

The current MVP uses system audio commands directly instead of adding a Python audio stack. This keeps runtime dependencies small and simplifies deployment on Raspberry Pi.

The current streaming wake path is a lightweight local voice-activity gate. The wakeword itself is still confirmed by matching MiMo ASR text. `voice-pet` does not reimplement PicoClaw agents; agents, memory, and tools remain PicoClaw responsibilities.
