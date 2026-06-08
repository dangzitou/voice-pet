from __future__ import annotations

import re
import secrets
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

from ..audio.capture import AudioCapture
from ..audio.player import AudioPlayer
from ..asr.mimo_asr import MimoASR
from ..brain.direct_llm import DirectLLMAdapter
from ..brain.picoclaw import PicoBridgeConfig, PicoClawAdapter
from ..tts.mimo_tts import MimoTTS
from .actions import build_default_router
from .picoclaw_gateway import PicoClawGatewayProcess
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
        self.tts = MimoTTS(
            api_key,
            mimo["api_base"],
            mimo["tts_model"],
            mimo.get("tts_voice", "mimo_default"),
            mimo.get("tts_format", "wav"),
            timeout,
            mimo.get("tts_style_prompt", ""),
        )
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
        self.player = AudioPlayer(
            command=audio.get("playback_command", "aplay"),
            device=audio.get("playback_device", ""),
        )
        self.gateway = PicoClawGatewayProcess(runtime)
        self.router = build_default_router() if runtime.get("enable_local_actions", False) else None
        self.detector = WakewordDetector(wakeword.get("aliases", []))
        self.ack_text = wakeword.get("ack_text", "主人，咋啦")
        ack_audio_path = str(wakeword.get("ack_audio_path", "")).strip()
        self.ack_audio_path = Path(ack_audio_path).expanduser() if ack_audio_path else None
        ack_texts = wakeword.get("ack_texts", [])
        if not isinstance(ack_texts, list):
            ack_texts = []
        self.ack_texts = [str(text).strip() for text in ack_texts if str(text).strip()]
        ack_audio_paths = wakeword.get("ack_audio_paths", [])
        if not isinstance(ack_audio_paths, list):
            ack_audio_paths = []
        self.ack_audio_paths = [Path(str(path)).expanduser() for path in ack_audio_paths if str(path).strip()]
        self.ack_variant_paths = self._prepare_ack_variant_paths()
        self.thinking_prompt_delay_seconds = float(wakeword.get("thinking_prompt_delay_seconds", 5.0))
        prompt_texts = wakeword.get("thinking_prompt_texts", [])
        if not isinstance(prompt_texts, list):
            prompt_texts = []
        self.thinking_prompt_texts = [str(text).strip() for text in prompt_texts if str(text).strip()]
        self.thinking_prompt_paths = self._prepare_thinking_prompt_paths()
        self.wake_max_extra_chars = int(wakeword.get("max_extra_chars", 6))
        self.cooldown_seconds = float(wakeword.get("cooldown_seconds", 3.0))
        self.session_timeout_seconds = float(wakeword.get("session_timeout_seconds", 60.0))
        self.listen_mode = str(audio.get("listen_mode", "streaming")).strip().lower()
        self.listen_window_seconds = float(audio.get("listen_window_seconds", 2.5))
        self.wake_max_seconds = _positive_float(audio.get("wake_max_seconds", 5.0), default=5.0)
        self.wake_min_seconds = float(audio.get("wake_min_seconds", 0.4))
        self.utterance_max_seconds = _positive_float(audio.get("utterance_max_seconds", 20.0), default=20.0)
        self.utterance_min_seconds = float(audio.get("utterance_min_seconds", 1.0))
        self.utterance_start_timeout_seconds = float(audio.get("utterance_start_timeout_seconds", 8.0))
        self.stream_chunk_ms = int(audio.get("stream_chunk_ms", 100))
        self.pre_roll_seconds = float(audio.get("pre_roll_seconds", 0.3))
        self.voice_start_threshold = int(audio.get("voice_start_threshold", audio.get("silence_threshold", 500)))
        self.silence_threshold = int(audio.get("silence_threshold", 500))
        self.silence_seconds = float(audio.get("silence_seconds", 1.2))
        self.wake_silence_seconds = float(audio.get("wake_silence_seconds", self.silence_seconds))
        self.utterance_silence_seconds = float(
            audio.get("utterance_silence_seconds", max(self.silence_seconds, 1.0))
        )
        self.playback_cooldown_seconds = max(0.0, float(audio.get("playback_cooldown_seconds", 0.5)))
        self.poll_interval_seconds = float(runtime.get("poll_interval_seconds", 0.2))
        self._last_wake_at = 0.0
        self._previous_idle_text = ""

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
            self._close_capture()
            self.gateway.stop()

    def run_once(self) -> None:
        now = time.monotonic()
        if now - self._last_wake_at < self.cooldown_seconds:
            time.sleep(self.poll_interval_seconds)
            return

        idle_path = self.work_dir / "wake-candidate.wav"
        self._record_wake_candidate(idle_path)
        self._reset_capture_stream()
        heard = self._time_call("idle_asr", self.asr.transcribe_file, str(idle_path))
        heard = heard.strip()
        if not heard:
            print("[idle] heard nothing")
            return

        print(f"[idle] asr={heard}")
        wake = self.detector.detect(heard)
        if not wake.matched and self._previous_idle_text:
            boundary_wake = self.detector.detect_boundary(self._previous_idle_text, heard)
            if boundary_wake.matched:
                print(f"[idle] boundary wake match previous={self._previous_idle_text} current={heard}")
                wake = boundary_wake
        self._previous_idle_text = "" if wake.matched else heard
        if not wake.matched:
            return
        if self.wake_max_extra_chars >= 0 and len(wake.cleaned_text) > self.wake_max_extra_chars:
            print(f"[idle] ignored wake match with extra text={wake.cleaned_text}")
            return

        self._previous_idle_text = ""
        self._last_wake_at = time.monotonic()
        print(f"[wake] matched alias={wake.alias}")
        self.play_ack()
        self._run_wake_session()

    def play_ack(self) -> None:
        ack_variant = self._pick_ack_variant()
        if ack_variant is not None:
            ack_path, ack_text = ack_variant
            try:
                self._ensure_ack_audio(ack_path, ack_text)
                print(f"[wake] ack_audio={ack_path}")
                self._play_file_sync("wake_ack", ack_path)
                return
            except Exception as exc:
                print(f"[wake] ack_audio_failed={exc}")
        if self.ack_audio_path and self.ack_audio_path.is_file():
            print(f"[wake] ack_audio={self.ack_audio_path}")
            self._play_file_sync("wake_ack", self.ack_audio_path)
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

            self._reset_capture_stream()
            user_text = self._time_call("session_asr", self.asr.transcribe_file, str(user_audio)).strip()
            if not user_text:
                print("[session] empty user text")
                continue
            session_wake = self.detector.detect_prefix(user_text)
            if not session_wake.matched:
                print(f"[session] ignored non-prefixed speech={user_text}")
                user_text = ""
                continue
            if not session_wake.cleaned_text:
                print(f"[session] ignored wakeword-only text={user_text}")
                user_text = ""
                continue
            user_text = session_wake.cleaned_text

    def _handle_user_text(self, user_text: str) -> None:
        print(f"[think] user={user_text}")
        local_reply = self.router.handle(user_text) if self.router else None
        reply = local_reply or self._reply_with_waiting_prompt(user_text)
        reply = _format_spoken_reply(reply)
        if not reply:
            reply = "主人，我刚刚没组织好，再说一次吧。"
        print(f"[speak] reply={reply}")
        self.say(reply, prefix="reply")

    def _reply_with_waiting_prompt(self, user_text: str) -> str:
        if self.thinking_prompt_delay_seconds <= 0 or not self.thinking_prompt_texts:
            return self._time_call("brain_reply", self.brain.reply, user_text)

        started_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.brain.reply, user_text)
            prompt_count = 0
            next_prompt_at = started_at + self.thinking_prompt_delay_seconds

            while True:
                try:
                    timeout_seconds = max(0.0, next_prompt_at - time.monotonic())
                    reply = future.result(timeout=timeout_seconds)
                    elapsed_ms = (time.monotonic() - started_at) * 1000
                    print(
                        f"[timing] brain_reply={elapsed_ms:.0f}ms "
                        f"prompt_count={prompt_count}"
                    )
                    return reply
                except FutureTimeoutError:
                    prompt_count += 1

                elapsed_ms = (time.monotonic() - started_at) * 1000
                print(
                    f"[timing] brain_reply_waited={elapsed_ms:.0f}ms "
                    f"prompt_delay={self.thinking_prompt_delay_seconds:.1f}s "
                    f"prompt_count={prompt_count}"
                )
                self.play_thinking_prompt()
                next_prompt_at += self.thinking_prompt_delay_seconds
                while next_prompt_at <= time.monotonic():
                    next_prompt_at += self.thinking_prompt_delay_seconds

    def play_thinking_prompt(self) -> None:
        prompt_path = self._pick_thinking_prompt_path()
        if prompt_path is None:
            return
        try:
            self._ensure_prompt_audio(prompt_path)
            print(f"[think] prompt_audio={prompt_path}")
            self._play_file_sync("think_prompt", prompt_path)
        except Exception as exc:
            print(f"[think] prompt_audio_failed={exc}")

    def _prepare_thinking_prompt_paths(self) -> list[Path]:
        prompt_dir = self.work_dir / "thinking-prompts"
        return [prompt_dir / f"prompt-{index + 1:02d}.wav" for index, _ in enumerate(self.thinking_prompt_texts)]

    def _prepare_ack_variant_paths(self) -> list[Path]:
        ack_dir = self.work_dir / "ack-variants"
        return [ack_dir / f"ack-{index + 1:02d}.wav" for index, _ in enumerate(self.ack_texts)]

    def _pick_ack_variant(self) -> tuple[Path, str] | None:
        variants: list[tuple[Path, str]] = []
        for path in self.ack_audio_paths:
            variants.append((path, ""))
        for index, path in enumerate(self.ack_variant_paths):
            variants.append((path, self.ack_texts[index]))
        if not variants:
            return None
        return secrets.choice(variants)

    def _pick_thinking_prompt_path(self) -> Path | None:
        if not self.thinking_prompt_paths:
            return None
        return secrets.choice(self.thinking_prompt_paths)

    def _ensure_ack_audio(self, path: Path, text: str) -> None:
        if path.is_file():
            return
        if not text:
            raise FileNotFoundError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        audio_bytes = self._time_call("wake_ack_tts", self.tts.synthesize, text)
        path.write_bytes(audio_bytes)

    def _ensure_prompt_audio(self, path: Path) -> None:
        if path.is_file():
            return
        index = self.thinking_prompt_paths.index(path)
        prompt_text = self.thinking_prompt_texts[index]
        path.parent.mkdir(parents=True, exist_ok=True)
        audio_bytes = self._time_call("think_prompt_tts", self.tts.synthesize, prompt_text)
        path.write_bytes(audio_bytes)

    def say(self, text: str, prefix: str) -> None:
        audio_bytes = self._time_call(f"{prefix}_tts", self.tts.synthesize, text)
        audio_path = self.work_dir / f"{prefix}.wav"
        audio_path.write_bytes(audio_bytes)
        self._play_file_sync(prefix, audio_path)

    def _play_file_sync(self, label: str, path: Path) -> None:
        self._reset_capture_stream()
        self._time_call(f"{label}_play", self.player.play_file, str(path))
        if self.playback_cooldown_seconds > 0:
            self._time_call(f"{label}_playback_cooldown", time.sleep, self.playback_cooldown_seconds)
        self._reset_capture_stream()

    def _reset_capture_stream(self) -> None:
        reset_stream = getattr(self.capture, "reset_stream", None)
        if callable(reset_stream):
            reset_stream()

    def _close_capture(self) -> None:
        close = getattr(self.capture, "close", None)
        if callable(close):
            close()

    def _record_wake_candidate(self, path: Path) -> None:
        if self.listen_mode == "fixed_window":
            self._time_call("wake_record", self.capture.record_for_duration, str(path), self.listen_window_seconds)
            return

        self._time_call(
            "wake_record",
            self.capture.record_next_utterance,
            str(path),
            max_seconds=self.wake_max_seconds,
            min_seconds=self.wake_min_seconds,
            chunk_ms=self.stream_chunk_ms,
            pre_roll_seconds=self.pre_roll_seconds,
            start_threshold=self.voice_start_threshold,
            silence_threshold=self.silence_threshold,
            silence_seconds=self.wake_silence_seconds,
        )

    def _record_user_utterance(self, path: Path, start_timeout_seconds: float | None = None) -> None:
        self._time_call(
            "user_record",
            self.capture.record_next_utterance,
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
            silence_seconds=self.utterance_silence_seconds,
        )

    def _time_call(self, label: str, func, *args, **kwargs):
        started_at = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.monotonic() - started_at) * 1000
            print(f"[timing] {label}={elapsed_ms:.0f}ms")


_EMOJI = re.compile(r"[\U00010000-\U0010ffff]")
_MARKDOWN_NOISE = re.compile(r"[*_`>#~\[\]]+")
_LEADING_FILLERS = re.compile(r"^(?:[哈啊嗯呃额呵嘿]{1,}|哈哈+|呵呵+|嘿嘿+)[，。,.!?！？；;：:\s]*")
_SPACES = re.compile(r"\s+")


def _format_spoken_reply(text: str) -> str:
    spoken = _clean_spoken_text(text)
    return spoken.strip()


def _positive_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _clean_spoken_text(text: str) -> str:
    text = _EMOJI.sub("", text)
    text = _MARKDOWN_NOISE.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _SPACES.sub(" ", text).strip()
    return _LEADING_FILLERS.sub("", text).strip()
