# voice-pet

`voice-pet` 是一个面向树莓派的本地语音助手原型。

它在 PicoClaw 之外独立运行，负责麦克风采集、唤醒词检测、语音转写、回复生成和本地播报；后续可以通过适配层接入 PicoClaw 的 agent、memory 和 tools。

## Features

- MiMo ASR / TTS / text model integration
- Local audio capture via `arecord`
- Local playback via `aplay`
- Wakeword alias matching (`小爱`, `小艾`, `小ai`, `xiao ai`, `xiaoai`)
- Single-turn voice interaction state machine
- Pluggable action router for weather, music, and device actions

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
├── main.py              # CLI entrypoint
├── state_machine.py     # runtime loop
├── audio_capture.py     # recording and silence cut-off
├── wakeword.py          # wakeword alias matching
├── action_router.py     # action routing hooks
├── asr/mimo_asr.py      # MiMo ASR client
├── tts/mimo_tts.py      # MiMo TTS client
└── brain/direct_llm.py  # MiMo text reply client
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

`config.json` can also carry the key, but environment variables are preferred.

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

The current MVP uses system audio commands directly instead of a Python audio stack. This keeps the runtime small and makes deployment on Raspberry Pi straightforward.

The next iteration should focus on streaming wakeword detection, interruption handling, and action integrations.
