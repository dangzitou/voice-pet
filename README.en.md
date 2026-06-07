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
pico_bridge_once.js            # Node WebSocket helper for PicoClaw bridge
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

## Configuration

Copy the example config:

```bash
cp ~/.picoclaw/voice-pet/config.example.json ~/.picoclaw/voice-pet/config.json
```

Set the API key via environment variable:

```bash
export MIMO_API_KEY="<your-token>"
export PICOCLAW_TOKEN="<your-pico-token>"
```

You can also place the key in `config.json`. If both are configured, environment variables override `config.json`.

You can also use the configuration CLI to create, inspect, and update voice model settings:

```bash
voice-pet config init --config ./config.json
voice-pet config show --config ./config.json
voice-pet config set --config ./config.json \
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

Supported voice model fields are: `--api-key`, `--clear-api-key`, `--api-base` / `--base-url`, `--model` / `--llm-model` / `--model-name`, `--asr-model` / `--asr-model-name`, `--tts-model` / `--tts-model-name`, `--tts-voice`, `--tts-format`, and `--language`. Audio playback can be configured with `--playback-command` and `--playback-device`. `show` only prints whether the API key is configured; it does not print the key.

To pin playback to a BlueALSA Bluetooth speaker, for example BT501:

```bash
voice-pet config set --config ./config.json \
  --playback-command aplay \
  --playback-device "bluealsa:DEV=D6:BF:DF:4A:EF:E2,PROFILE=a2dp,VOL=100+"
```

To prebuild the `主人，咋啦` wake acknowledgement, synthesize it once and then save the path in config:

```bash
voice-pet demo --config ./config.json \
  --text "主人，咋啦" \
  --output ~/.picoclaw/voice-pet/runtime/ack.wav
voice-pet config set --config ./config.json \
  --ack-text "主人，咋啦" \
  --ack-audio-path ~/.picoclaw/voice-pet/runtime/ack.wav
```

When `wakeword.ack_audio_path` is configured, wake acknowledgement directly plays that file. If the file is missing, it falls back to TTS synthesis from `wakeword.ack_text`.

To let `voice-pet` start PicoClaw gateway:

```bash
voice-pet config set --config ./config.json \
  --manage-picoclaw-gateway \
  --picoclaw-gateway-command picoclaw \
  --picoclaw-gateway-args "gateway" \
  --picoclaw-gateway-ready-url http://127.0.0.1:18790/ready
```

By default, `voice-pet` connects to an already-running gateway. The PicoClaw token is not written by the CLI; keep using the `PICOCLAW_TOKEN` environment variable.

The default listener is local streaming mode:

- `audio.listen_mode = "streaming"`: continuously reads microphone audio and calls ASR only after speech is detected
- `audio.listen_mode = "fixed_window"`: switches back to the previous fixed-window recording mode for microphone or threshold debugging
- `audio.voice_start_threshold` / `audio.silence_threshold`: control speech start and silence cutoff thresholds
- `audio.stream_chunk_ms` / `audio.pre_roll_seconds`: control streaming chunk size and preserved pre-trigger audio
- `wakeword.session_timeout_seconds = 60.0`: leave wake mode after 60 seconds without new speech
- `wakeword.ack_audio_path`: prebuilt wake acknowledgement audio path, played directly when configured

## Run

Install dependencies:

```bash
pip install -r requirements.txt
npm install
```

Start the full local system:

```bash
cd /home/zitou/my-project/voice-pet
voice-pet start
```

Check status, follow logs, and stop:

| Command | Purpose |
| --- | --- |
| `voice-pet start` | Start PicoClaw gateway and voice-pet in the background |
| `voice-pet stop` | Stop voice-pet and PicoClaw gateway |
| `voice-pet restart` | Restart the full voice runtime |
| `voice-pet status` | Show gateway/voice-pet process and health status |
| `voice-pet logs -f` | Follow voice-pet logs |
| `voice-pet logs --target gateway -f` | Follow PicoClaw gateway logs |
| `voice-pet config show` | Show current model, audio, and runtime configuration |
| `voice-pet config set --tts-voice 冰糖` | Update config, example switches TTS voice |
| `voice-pet mock --offline --wake-text "小爱小爱" --user-text "今天厦门天气咋样"` | Run the offline mock end-to-end test |
| `voice-pet demo --text "主人，咋啦"` | Run a MiMo TTS/ASR debug demo |

Most common status, log, and stop commands:

```bash
voice-pet status
voice-pet logs -f
voice-pet stop
```

`start` launches both the PicoClaw gateway and voice-pet in the background, loads `~/.picoclaw/voice-pet/voice-pet.env`, and removes local proxy environment variables so localhost gateway traffic is not routed through a proxy. Logs are written to:

```text
~/.picoclaw/voice-pet/runtime/voice-pet.log
~/.picoclaw/logs/gateway-voice-pet.log
```

For debugging, you can still run only the main loop:

```bash
PYTHONPATH=src python3 -m voice_pet.main --config ~/.picoclaw/voice-pet/config.json
```

Run the TTS / ASR demo:

```bash
voice-pet demo --text "主人，咋啦"
```

Run the mock end-to-end test:

```bash
voice-pet mock --wake-text "小爱小爱" --user-text "你好，请只回复：ok"
```

Run the offline mock without MiMo/PicoClaw credentials:

```bash
voice-pet mock --offline --wake-text "小爱小爱" --user-text "今天天气怎么样"
```

## Runtime Flow

1. Continuously read microphone PCM locally
2. Capture a wake candidate after a full speech segment is detected
3. Transcribe the candidate audio with MiMo ASR
4. Match wakeword aliases
5. If the wakeword matches, immediately acknowledge with `主人，咋啦`
6. Enter wake mode and continue waiting for user speech
7. Transcribe user speech with MiMo ASR and forward the text to PicoClaw gateway
8. Receive the reply text from PicoClaw
9. Synthesize the reply with MiMo TTS and play it locally
10. Continue waiting for the next user utterance; leave wake mode after 60 seconds without speech

## Notes

The current MVP uses system audio commands directly instead of adding a Python audio stack. This keeps runtime dependencies small and simplifies deployment on Raspberry Pi.

The current streaming wake path is a lightweight local voice-activity gate. The wakeword itself is still confirmed by matching MiMo ASR text. `voice-pet` does not reimplement PicoClaw agents; agents, memory, and tools remain PicoClaw responsibilities.
