from __future__ import annotations

import time
from pathlib import Path

from .action_router import build_default_router
from .audio_capture import AudioCapture
from .asr.mimo_asr import MimoASR
from .brain.direct_llm import DirectLLMAdapter
from .player import AudioPlayer
from .tts.mimo_tts import MimoTTS
from .wakeword import WakewordDetector


class VoicePetStateMachine:
    def __init__(self, config: dict):
        self.config = config
        mimo = config["mimo"]
        audio = config["audio"]
        wakeword = config["wakeword"]
        runtime = config["runtime"]

        api_key = mimo.get("api_key", "").strip()
        if not api_key:
            raise ValueError("missing MIMO_API_KEY or config mimo.api_key")

        self.work_dir = Path(runtime["work_dir"])
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.capture = AudioCapture(
            sample_rate=audio["sample_rate"],
            channels=audio["channels"],
            device=audio.get("record_device", ""),
            silence_threshold=audio.get("silence_threshold", 500),
            silence_seconds=audio.get("silence_seconds", 1.2),
        )
        timeout = int(runtime.get("request_timeout_seconds", 120))
        self.asr = MimoASR(api_key, mimo["api_base"], mimo["asr_model"], mimo.get("language", "zh"), timeout)
        self.tts = MimoTTS(api_key, mimo["api_base"], mimo["tts_model"], mimo.get("tts_voice", "default_zh"), mimo.get("tts_format", "wav"), timeout)
        self.brain = DirectLLMAdapter(api_key, mimo["api_base"], mimo["llm_model"], timeout)
        self.player = AudioPlayer()
        self.router = build_default_router()
        self.detector = WakewordDetector(wakeword.get("aliases", []))
        self.ack_text = wakeword.get("ack_text", "主人，咋啦")
        self.cooldown_seconds = float(wakeword.get("cooldown_seconds", 3.0))
        self.listen_window_seconds = float(audio.get("listen_window_seconds", 2.5))
        self.utterance_max_seconds = float(audio.get("utterance_max_seconds", 8))
        self.utterance_min_seconds = float(audio.get("utterance_min_seconds", 1.0))
        self.poll_interval_seconds = float(runtime.get("poll_interval_seconds", 0.2))
        self._last_wake_at = 0.0

    def run(self) -> None:
        print("[voice-pet] started, waiting for wakeword...")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[voice-pet] loop error: {exc}")
                time.sleep(1.0)

    def run_once(self) -> None:
        now = time.monotonic()
        if now - self._last_wake_at < self.cooldown_seconds:
            time.sleep(self.poll_interval_seconds)
            return

        idle_path = self.work_dir / "idle-listen.wav"
        self.capture.record_for_duration(str(idle_path), self.listen_window_seconds)
        heard = self.asr.transcribe_file(str(idle_path))
        heard = heard.strip()
        if not heard:
            print("[idle] heard nothing")
            return

        print(f"[idle] asr={heard}")
        wake = self.detector.detect(heard)
        if not wake.matched:
            return

        self._last_wake_at = time.monotonic()
        print(f"[wake] matched alias={wake.alias}")
        self.say(self.ack_text, prefix="ack")

        if wake.cleaned_text:
            user_text = wake.cleaned_text
        else:
            user_audio = self.work_dir / "user-utterance.wav"
            self.capture.record_until_silence(
                str(user_audio),
                max_seconds=self.utterance_max_seconds,
                min_seconds=self.utterance_min_seconds,
            )
            user_text = self.asr.transcribe_file(str(user_audio)).strip()

        if not user_text:
            print("[think] empty user text")
            return

        print(f"[think] user={user_text}")
        reply = self.router.handle(user_text) or self.brain.reply(user_text)
        reply = reply.strip()
        if not reply:
            reply = "主人，我刚刚没组织好，再说一次吧。"
        print(f"[speak] reply={reply}")
        self.say(reply, prefix="reply")

    def say(self, text: str, prefix: str) -> None:
        audio_bytes = self.tts.synthesize(text)
        audio_path = self.work_dir / f"{prefix}.wav"
        audio_path.write_bytes(audio_bytes)
        self.player.play_file(str(audio_path))
