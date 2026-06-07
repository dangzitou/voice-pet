from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .asr.mimo_asr import MimoASR
from .tts.mimo_tts import MimoTTS
from .brain.direct_llm import DirectLLMAdapter
from .brain.picoclaw import PicoBridgeConfig, PicoClawAdapter
from .wakeword import WakewordDetector
from .action_router import build_default_router
from .player import AudioPlayer


def main() -> None:
    parser = argparse.ArgumentParser(description="Run voice-pet MVP with mock TTS-generated input")
    parser.add_argument("--config", default="~/.picoclaw/voice-pet/config.json")
    parser.add_argument("--wake-text", default="小爱小爱")
    parser.add_argument("--user-text", default="我今天心情不好")
    parser.add_argument("--play", action="store_true", help="play generated audio locally")
    args = parser.parse_args()

    cfg = load_config(args.config)
    mimo = cfg["mimo"]
    runtime = cfg["runtime"]
    wakeword_cfg = cfg["wakeword"]
    api_key = mimo.get("api_key", "").strip()
    if not api_key:
        raise ValueError("missing MIMO_API_KEY or config mimo.api_key")

    work_dir = Path(runtime["work_dir"]) / "mock-test"
    work_dir.mkdir(parents=True, exist_ok=True)
    timeout = int(runtime.get("request_timeout_seconds", 120))

    tts = MimoTTS(api_key, mimo["api_base"], mimo["tts_model"], mimo.get("tts_voice", "mimo_default"), mimo.get("tts_format", "wav"), timeout)
    asr = MimoASR(api_key, mimo["api_base"], mimo["asr_model"], mimo.get("language", "zh"), timeout)
    brain_kind = str(runtime.get("brain", "direct_llm")).strip().lower()
    if brain_kind == "picoclaw":
        brain = PicoClawAdapter(
            PicoBridgeConfig(
                url=runtime["picoclaw_ws_url"],
                token=runtime["picoclaw_token"],
                session_id=runtime.get("picoclaw_session_id", "voice-pet"),
                timeout_seconds=float(runtime.get("request_timeout_seconds", 120)),
            )
        )
    else:
        brain = DirectLLMAdapter(api_key, mimo["api_base"], mimo["llm_model"], timeout)
    detector = WakewordDetector(wakeword_cfg.get("aliases", []))
    router = build_default_router()
    player = AudioPlayer()

    wake_audio = work_dir / "wake.wav"
    wake_audio.write_bytes(tts.synthesize(args.wake_text))
    wake_asr = asr.transcribe_file(str(wake_audio)).strip()
    wake_result = detector.detect(wake_asr)

    print(f"wake_text: {args.wake_text}")
    print(f"wake_asr: {wake_asr}")
    print(f"wake_matched: {wake_result.matched}")
    print(f"wake_alias: {wake_result.alias}")

    if not wake_result.matched:
        print("mock wakeword did not match; aborting")
        return

    ack_text = wakeword_cfg.get("ack_text", "主人，咋啦")
    ack_audio = work_dir / "ack.wav"
    ack_audio.write_bytes(tts.synthesize(ack_text))
    if args.play:
        player.play_file(str(ack_audio))

    user_audio = work_dir / "user.wav"
    user_audio.write_bytes(tts.synthesize(args.user_text))
    user_asr = asr.transcribe_file(str(user_audio)).strip()
    reply_text = router.handle(user_asr) or brain.reply(user_asr)

    reply_audio = work_dir / "reply.wav"
    reply_audio.write_bytes(tts.synthesize(reply_text))

    print(f"user_text: {args.user_text}")
    print(f"user_asr: {user_asr}")
    print(f"reply_text: {reply_text}")
    print(f"artifacts_dir: {work_dir}")

    if args.play:
        player.play_file(str(reply_audio))


if __name__ == "__main__":
    main()
