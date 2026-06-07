# voice-pet

`voice-pet` is a Raspberry Pi voice assistant prototype.

It runs outside PicoClaw and handles microphone capture, wakeword detection, speech transcription, reply generation, and local playback. A future adapter layer can connect it to PicoClaw agents, memory, and tools.

中文说明： [README.md](./README.md)

## Features

- MiMo ASR, TTS, and text model integration
- Local audio capture via `arecord`
- Local streaming voice activity detection to reduce idle ASR requests
- Local playback via `aplay`
- Wakeword alias matching: `小爱`, `小艾`, `小ai`, `xiao ai`, `xiaoai`
- Single-turn voice interaction state machine
- Pluggable action router for weather, music, and device actions
- Optional PicoClaw gateway backend for reply generation

## Status

Implemented in the current MVP:

- end-to-end loop: listen -> transcribe -> reply -> synthesize -> play
- minimal runtime state machine: `idle -> wake -> record -> think -> speak -> idle`
- local streaming voice activity detection: continuously reads microphone PCM and calls ASR only after a complete speech segment is captured
- MiMo adapters for ASR, TTS, and text reply generation

Not implemented yet:

- offline keyword wakeword engine
- multi-turn session management
- concrete weather / music integrations
- PicoClaw adapter

## Project Layout

```text
src/voice_pet/
├── main.py                # CLI entrypoint
├── config_cli.py          # configuration CLI
├── state_machine.py       # runtime loop
├── audio_capture.py       # recording and silence cut-off
├── wakeword.py            # wakeword alias matching
├── action_router.py       # action routing hooks
├── mock_mvp.py            # mock end-to-end test using TTS-generated input
├── asr/mimo_asr.py        # MiMo ASR client
├── tts/mimo_tts.py        # MiMo TTS client
├── brain/direct_llm.py    # direct MiMo text reply backend
└── brain/picoclaw.py      # PicoClaw gateway bridge adapter
pico_bridge_once.js        # Node WebSocket helper for PicoClaw bridge
```

## Requirements

- Python 3.13+
- ALSA tools:
  - `arecord`
  - `aplay`
- MiMo API key

## Configuration

Copy the example config:

```bash
cp ~/.picoclaw/voice-pet/config.example.json ~/.picoclaw/voice-pet/config.json
```

Set the API key via environment variable:

```bash
export MIMO_API_KEY="<your-token>"
```

You can also place the key in `config.json`, though environment variables are preferred.

You can also use the configuration CLI to create, inspect, and update model settings:

```bash
PYTHONPATH=src python3 -m voice_pet.config_cli init --config ./config.json
PYTHONPATH=src python3 -m voice_pet.config_cli show --config ./config.json
PYTHONPATH=src python3 -m voice_pet.config_cli set --config ./config.json \
  --model mimo-v2.5 \
  --asr-model mimo-v2.5-asr \
  --tts-model mimo-v2.5-tts \
  --tts-voice mimo_default \
  --language zh
```

Supported model fields are: `--api-base`, `--model` / `--llm-model`, `--asr-model`, `--tts-model`, `--tts-voice`, `--tts-format`, and `--language`.

The default listener is local streaming mode:

- `audio.listen_mode = "streaming"`: continuously reads microphone audio and calls ASR only after speech is detected
- `audio.listen_mode = "fixed_window"`: switches back to the previous fixed-window recording mode for microphone or threshold debugging
- `audio.voice_start_threshold` / `audio.silence_threshold`: control speech start and silence cutoff thresholds
- `audio.stream_chunk_ms` / `audio.pre_roll_seconds`: control streaming chunk size and preserved pre-trigger audio

If you want PicoClaw to be the reply backend, also set these fields in `config.json`:

- `runtime.brain = "picoclaw"`
- `runtime.picoclaw_ws_url`
- `runtime.picoclaw_token`
- `runtime.picoclaw_session_id`

## Run

Start the main loop:

```bash
cd ~/.picoclaw/voice-pet
PYTHONPATH=src python3 -m voice_pet.main --config ~/.picoclaw/voice-pet/config.json
```

Run the TTS / ASR demo:

```bash
cd ~/.picoclaw/voice-pet
PYTHONPATH=src python3 -m voice_pet.demo_loop --config ~/.picoclaw/voice-pet/config.json --text "主人，咋啦"
```

Run the mock end-to-end test:

```bash
cd ~/.picoclaw/voice-pet
PYTHONPATH=src python3 -m voice_pet.mock_mvp --config ~/.picoclaw/voice-pet/config.json --wake-text "小爱小爱" --user-text "你好，请只回复：ok"
```

## Runtime Flow

1. Continuously read microphone PCM locally
2. Capture a wake candidate after a full speech segment is detected
3. Transcribe the candidate audio with MiMo ASR
4. Match wakeword aliases
5. Acknowledge with `主人，咋啦`
6. Stream-record the user utterance until silence or the maximum duration
7. Route to an action handler or text model
8. Synthesize the reply with MiMo TTS
9. Play the result locally

## Notes

The current MVP uses system audio commands directly instead of adding a Python audio stack. This keeps runtime dependencies small and simplifies deployment on Raspberry Pi.

The current streaming wake path is a lightweight local voice-activity gate. The wakeword itself is still confirmed by matching MiMo ASR text. The next iteration should focus on an offline keyword engine, interruption handling, and action integrations.
