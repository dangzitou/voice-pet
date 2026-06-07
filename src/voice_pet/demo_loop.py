from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .asr.mimo_asr import MimoASR
from .tts.mimo_tts import MimoTTS
from .player import AudioPlayer


def main() -> None:
    parser = argparse.ArgumentParser(description="MiMo local loop demo")
    parser.add_argument("--config", default="~/.picoclaw/voice-pet/config.json")
    parser.add_argument("--text", default="主人咋啦")
    parser.add_argument("--output", default="~/.picoclaw/voice-pet/runtime/demo_tts.wav")
    parser.add_argument("--transcribe", default="", help="如果提供音频文件，则额外做一次 ASR")
    args = parser.parse_args()

    cfg = load_config(args.config)
    mimo = cfg["mimo"]
    runtime = cfg["runtime"]
    api_key = mimo.get("api_key", "").strip()
    if not api_key:
        raise ValueError("missing MIMO_API_KEY or config mimo.api_key")

    timeout = int(runtime.get("request_timeout_seconds", 120))
    tts = MimoTTS(api_key, mimo["api_base"], mimo["tts_model"], mimo.get("tts_voice", "mimo_default"), mimo.get("tts_format", "wav"), timeout)
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(tts.synthesize(args.text))
    print(f"tts written: {output_path}")

    AudioPlayer().play_file(str(output_path))

    target = args.transcribe.strip()
    if target:
        asr = MimoASR(api_key, mimo["api_base"], mimo["asr_model"], mimo.get("language", "zh"), timeout)
        text = asr.transcribe_file(target)
        print(f"asr text: {text}")


if __name__ == "__main__":
    main()
