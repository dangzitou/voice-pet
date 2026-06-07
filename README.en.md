# voice-pet

`voice-pet` is a Raspberry Pi voice assistant prototype.

It runs outside PicoClaw and handles microphone capture, wakeword detection, speech transcription, reply generation, and local playback. A future adapter layer can connect it to PicoClaw agents, memory, and tools.

中文说明： [README.md](./README.md)

## Features

- MiMo ASR, TTS, and text model integration
- Local audio capture via `arecord`
- Local playback via `aplay`
- Wakeword alias matching: `小爱`, `小艾`, `小ai`, `xiao ai`, `xiaoai`
- Single-turn voice interaction state machine
- Pluggable action router for weather, music, and device actions
- Optional PicoClaw gateway backend for reply generation

## Status

Implemented in the current MVP:

- end-to-end loop: listen -> transcribe -> reply -> synthesize -> play
- minimal runtime state machine: `idle -> wake -> record -> think -> speak -> idle`
- MiMo adapters for ASR, TTS, and text reply generation

Not implemented yet:

- streaming VAD / offline wakeword engine
- multi-turn session management
- concrete weather / music integrations
- PicoClaw adapter

## Project Layout

```text
src/voice_pet/
├── main.py                # CLI entrypoint
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

1. Record a short idle window
2. Transcribe audio with MiMo ASR
3. Match wakeword aliases
4. Acknowledge with `主人，咋啦`
5. Record the user utterance
6. Route to an action handler or text model
7. Synthesize the reply with MiMo TTS
8. Play the result locally

## Notes

The current MVP uses system audio commands directly instead of adding a Python audio stack. This keeps runtime dependencies small and simplifies deployment on Raspberry Pi.

The next iteration should focus on streaming wakeword detection, interruption handling, and action integrations.
