from __future__ import annotations

import time
from pathlib import Path

from .action_router import build_default_router
from .audio_capture import AudioCapture
from .asr.mimo_asr import MimoASR
from .brain.direct_llm import DirectLLMAdapter
from .brain.picoclaw import PicoBridgeConfig, PicoClawAdapter
from .player import AudioPlayer
from .picoclaw_gateway import PicoClawGatewayProcess
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
        self.tts = MimoTTS(api_key, mimo["api_base"], mimo["tts_model"], mimo.get("tts_voice", "mimo_default"), mimo.get("tts_format", "wav"), timeout)
        brain_kind = str(runtime.get("brain", "picoclaw")).strip().lower()
        if brain_kind == "picoclaw":
            token = runtime.get("picoclaw_token", "").strip()
            if not token:
                raise ValueError("missing PICOCLAW_TOKEN or config runtime.picoclaw_token")
            self.brain = PicoClawAdapter(
                PicoBridgeConfig(
                    url=runtime["picoclaw_ws_url"],
                    token=token,
                    session_id=runtime.get("picoclaw_session_id", "voice-pet"),
                    timeout_seconds=float(runtime.get("request_timeout_seconds", 120)),
                    node_script=runtime.get("picoclaw_node_script", "~/.picoclaw/voice-pet/pico_bridge_once.js"),
                )
            )
        elif brain_kind == "direct_llm":
            self.brain = DirectLLMAdapter(api_key, mimo["api_base"], mimo["llm_model"], timeout)
        else:
            raise ValueError(f"unsupported runtime.brain: {brain_kind}")
        self.player = AudioPlayer()
        self.gateway = PicoClawGatewayProcess(runtime)
        self.router = build_default_router() if runtime.get("enable_local_actions", False) else None
        self.detector = WakewordDetector(wakeword.get("aliases", []))
        self.ack_text = wakeword.get("ack_text", "主人咋啦")
        ack_audio_path = str(wakeword.get("ack_audio_path", "")).strip()
        self.ack_audio_path = Path(ack_audio_path).expanduser() if ack_audio_path else None
        self.cooldown_seconds = float(wakeword.get("cooldown_seconds", 3.0))
        self.session_timeout_seconds = float(wakeword.get("session_timeout_seconds", 60.0))
        self.listen_mode = str(audio.get("listen_mode", "streaming")).strip().lower()
        self.listen_window_seconds = float(audio.get("listen_window_seconds", 2.5))
        self.wake_max_seconds = float(audio.get("wake_max_seconds", 5.0))
        self.wake_min_seconds = float(audio.get("wake_min_seconds", 0.4))
        self.utterance_max_seconds = float(audio.get("utterance_max_seconds", 8))
        self.utterance_min_seconds = float(audio.get("utterance_min_seconds", 1.0))
        self.utterance_start_timeout_seconds = float(audio.get("utterance_start_timeout_seconds", 8.0))
        self.stream_chunk_ms = int(audio.get("stream_chunk_ms", 100))
        self.pre_roll_seconds = float(audio.get("pre_roll_seconds", 0.3))
        self.voice_start_threshold = int(audio.get("voice_start_threshold", audio.get("silence_threshold", 500)))
        self.silence_threshold = int(audio.get("silence_threshold", 500))
        self.silence_seconds = float(audio.get("silence_seconds", 1.2))
        self.poll_interval_seconds = float(runtime.get("poll_interval_seconds", 0.2))
        self._last_wake_at = 0.0

    def run(self) -> None:
        print(f"[voice-pet] started, waiting for wakeword... mode={self.listen_mode}")
        self.gateway.start()
        try:
            while True:
                try:
                    self.run_once()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[voice-pet] loop error: {exc}")
                    time.sleep(1.0)
        finally:
            self.gateway.stop()

    def run_once(self) -> None:
        now = time.monotonic()
        if now - self._last_wake_at < self.cooldown_seconds:
            time.sleep(self.poll_interval_seconds)
            return

        idle_path = self.work_dir / "wake-candidate.wav"
        self._record_wake_candidate(idle_path)
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
        self.play_ack()
        self._run_wake_session(wake.cleaned_text)

    def play_ack(self) -> None:
        if self.ack_audio_path and self.ack_audio_path.is_file():
            print(f"[wake] ack_audio={self.ack_audio_path}")
            self.player.play_file(str(self.ack_audio_path))
            return
        if self.ack_audio_path:
            print(f"[wake] ack_audio missing, fallback_tts={self.ack_audio_path}")
        self.say(self.ack_text, prefix="ack")

    def _run_wake_session(self, initial_text: str = "") -> None:
        print(f"[session] active, idle_timeout={self.session_timeout_seconds:.1f}s")
        deadline = time.monotonic() + self.session_timeout_seconds
        user_text = initial_text.strip()

        while True:
            if user_text:
                self._handle_user_text(user_text)
                deadline = time.monotonic() + self.session_timeout_seconds
                user_text = ""
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[session] idle timeout, waiting for wakeword")
                return

            user_audio = self.work_dir / "user-utterance.wav"
            try:
                self._record_user_utterance(user_audio, start_timeout_seconds=remaining)
            except TimeoutError:
                print("[session] no user speech before timeout, waiting for wakeword")
                return

            user_text = self.asr.transcribe_file(str(user_audio)).strip()
            if not user_text:
                print("[session] empty user text")

    def _handle_user_text(self, user_text: str) -> None:
        print(f"[think] user={user_text}")
        local_reply = self.router.handle(user_text) if self.router else None
        reply = local_reply or self.brain.reply(user_text)
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

    def _record_wake_candidate(self, path: Path) -> None:
        if self.listen_mode == "fixed_window":
            self.capture.record_for_duration(str(path), self.listen_window_seconds)
            return

        self.capture.record_next_utterance(
            str(path),
            max_seconds=self.wake_max_seconds,
            min_seconds=self.wake_min_seconds,
            chunk_ms=self.stream_chunk_ms,
            pre_roll_seconds=self.pre_roll_seconds,
            start_threshold=self.voice_start_threshold,
            silence_threshold=self.silence_threshold,
            silence_seconds=self.silence_seconds,
        )

    def _record_user_utterance(self, path: Path, start_timeout_seconds: float | None = None) -> None:
        self.capture.record_next_utterance(
            str(path),
            max_seconds=self.utterance_max_seconds,
            min_seconds=self.utterance_min_seconds,
            chunk_ms=self.stream_chunk_ms,
            pre_roll_seconds=self.pre_roll_seconds,
            start_timeout_seconds=(
                self.utterance_start_timeout_seconds
                if start_timeout_seconds is None
                else max(0.1, start_timeout_seconds)
            ),
            start_threshold=self.voice_start_threshold,
            silence_threshold=self.silence_threshold,
            silence_seconds=self.silence_seconds,
        )
