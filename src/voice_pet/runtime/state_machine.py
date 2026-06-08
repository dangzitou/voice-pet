from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

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
        music = config.get("music", {})
        if not isinstance(music, dict):
            music = {}
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
        pause_prompt_texts = _text_list(music.get("pause_prompt_texts"), DEFAULT_MUSIC_PAUSE_PROMPT_TEXTS)
        self.music_pause_prompt_texts = pause_prompt_texts
        self.music_pause_prompt_audio_paths = [
            Path(str(path)).expanduser()
            for path in _text_list(music.get("pause_prompt_audio_paths"), [])
        ]
        self.music_pause_prompt_paths = self._prepare_music_pause_prompt_paths()
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
        self.external_audio_defer_file = self.work_dir / "external-audio-defer.json"
        self.external_audio_defer_seconds = float(runtime.get("external_audio_defer_seconds", 180.0))
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
        self._previous_idle_text = ""
        self._last_wake_at = time.monotonic()
        if self._is_external_audio_active():
            self._handle_external_audio_speech(wake.cleaned_text, source="idle")
            return
        if self.wake_max_extra_chars >= 0 and len(wake.cleaned_text) > self.wake_max_extra_chars:
            print(f"[idle] ignored wake match with extra text={wake.cleaned_text}")
            return
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
            if self._is_external_audio_active():
                self._handle_external_audio_speech(session_wake.cleaned_text, source="session")
                user_text = ""
                deadline = time.monotonic() + self.session_timeout_seconds
                continue
            user_text = session_wake.cleaned_text

    def _handle_user_text(self, user_text: str) -> None:
        if self._is_external_audio_active():
            self._handle_external_audio_speech(user_text, source="handler")
            return
        print(f"[think] user={user_text}")
        defer_ready_path = begin_external_audio_defer(
            self.work_dir,
            user_text,
            defer_file=self.external_audio_defer_file,
            timeout_seconds=self.external_audio_defer_seconds,
        )
        try:
            local_reply = self.router.handle(user_text) if self.router else None
            reply = local_reply or self._reply_with_waiting_prompt(user_text)
            reply = _format_spoken_reply(reply)
            if not reply:
                reply = "主人，我刚刚没组织好，再说一次吧。"
            print(f"[speak] reply={reply}")
            self.say(reply, prefix="reply")
        finally:
            if defer_ready_path is not None:
                release_external_audio_defer(defer_ready_path, defer_file=self.external_audio_defer_file)

    def _is_external_audio_active(self) -> bool:
        status = self._external_audio_status()
        return status not in {"", "stopped"}

    def _external_audio_status(self) -> str:
        try:
            result = subprocess.run(
                ["ncm-cli", "state"],
                capture_output=True,
                check=False,
                text=True,
                timeout=1.5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[music] state_check_failed={exc}")
            return ""
        if result.returncode != 0:
            stderr = result.stderr.strip()
            print(f"[music] state_check_failed code={result.returncode} stderr={stderr}")
            return ""
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"[music] state_check_invalid={result.stdout.strip()}")
            return ""
        state = payload.get("state") if isinstance(payload, dict) else {}
        if not isinstance(state, dict):
            return ""
        return str(state.get("status", "")).strip().lower()

    def _handle_external_audio_speech(self, text: str, *, source: str) -> None:
        action = _external_audio_control_action(text)
        if action is None:
            print(f"[music] ignored while external audio active source={source} text={text}")
            return

        print(f"[music] control source={source} action={action} text={text}")
        try:
            result = self._time_call(
                f"music_{action}",
                subprocess.run,
                ["ncm-cli", action],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[music] control_failed action={action} error={exc}")
            return
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        print(f"[music] control_result action={action} code={result.returncode} stdout={stdout} stderr={stderr}")
        if action == "pause" and result.returncode == 0:
            self.play_music_pause_prompt()

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

    def play_music_pause_prompt(self) -> None:
        prompt_path = self._pick_music_pause_prompt_path()
        if prompt_path is None:
            return
        try:
            self._ensure_music_pause_prompt_audio(prompt_path)
            print(f"[music] pause_prompt_audio={prompt_path}")
            self._play_file_sync("music_pause_prompt", prompt_path)
        except Exception as exc:
            print(f"[music] pause_prompt_failed={exc}")

    def _prepare_thinking_prompt_paths(self) -> list[Path]:
        prompt_dir = self.work_dir / "thinking-prompts"
        return [prompt_dir / f"prompt-{index + 1:02d}.wav" for index, _ in enumerate(self.thinking_prompt_texts)]

    def _prepare_ack_variant_paths(self) -> list[Path]:
        ack_dir = self.work_dir / "ack-variants"
        return [ack_dir / f"ack-{index + 1:02d}.wav" for index, _ in enumerate(self.ack_texts)]

    def _prepare_music_pause_prompt_paths(self) -> list[Path]:
        prompt_dir = self.work_dir / "music-control-prompts"
        return [
            prompt_dir / f"pause-{index + 1:02d}.wav"
            for index, _ in enumerate(self.music_pause_prompt_texts)
        ]

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

    def _pick_music_pause_prompt_path(self) -> Path | None:
        variants = list(self.music_pause_prompt_audio_paths) + list(self.music_pause_prompt_paths)
        if not variants:
            return None
        return secrets.choice(variants)

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

    def _ensure_music_pause_prompt_audio(self, path: Path) -> None:
        if path.is_file():
            return
        if path not in self.music_pause_prompt_paths:
            raise FileNotFoundError(path)
        index = self.music_pause_prompt_paths.index(path)
        prompt_text = self.music_pause_prompt_texts[index]
        path.parent.mkdir(parents=True, exist_ok=True)
        audio_bytes = self._time_call("music_pause_prompt_tts", self.tts.synthesize, prompt_text)
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
_EXTERNAL_AUDIO_REQUEST = re.compile(r"(?:播放|放首|放一首|点歌|点一首|来一首|听歌|听音乐|播歌|放歌)")
_EXTERNAL_AUDIO_STOP_REQUEST = re.compile(r"(?:停止播放|暂停播放|关闭音乐|关掉音乐|停止音乐|暂停音乐|停歌|别放|不要放)")
_EXTERNAL_AUDIO_STOP_CONTROL = re.compile(r"(?:停止播放|关闭音乐|关掉音乐|停止音乐|停歌|停止|关掉|别放|不要放)")
_EXTERNAL_AUDIO_PAUSE_CONTROL = re.compile(r"(?:暂停播放|暂停音乐|暂停|先停一下|等一下)")
_EXTERNAL_AUDIO_RESUME_CONTROL = re.compile(r"(?:继续播放|恢复播放|继续音乐|恢复音乐|继续|恢复|接着放)")
DEFAULT_MUSIC_PAUSE_PROMPT_TEXTS = [
    "已经暂停啦，主人。",
    "暂停好啦，主人。",
    "好哒，已经帮你暂停啦。",
    "主人，音乐先暂停啦。",
    "收到，已经暂停播放啦。",
    "暂停啦，主人你说。",
    "好，先暂停啦。",
    "已经先停下啦，主人。",
    "音乐暂停啦，主人。",
    "好啦主人，已经暂停。",
]


def _format_spoken_reply(text: str) -> str:
    spoken = _clean_spoken_text(text)
    return spoken.strip()


def begin_external_audio_defer(
    work_dir: Path,
    user_text: str,
    *,
    defer_file: Path | None = None,
    timeout_seconds: float = 180.0,
) -> Path | None:
    if not _is_external_audio_request(user_text):
        return None

    work_dir.mkdir(parents=True, exist_ok=True)
    defer_path = defer_file or work_dir / "external-audio-defer.json"
    request_id = secrets.token_hex(8)
    ready_path = work_dir / f"external-audio-ready-{request_id}.signal"
    try:
        ready_path.unlink(missing_ok=True)
        payload = {
            "request_id": request_id,
            "ready_file": str(ready_path),
            "created_at": time.time(),
            "expires_at": time.time() + max(1.0, timeout_seconds),
        }
        _write_json_atomic(defer_path, payload)
        print(f"[audio] external_audio_defer={defer_path} ready={ready_path}")
        return ready_path
    except Exception as exc:
        print(f"[audio] external_audio_defer_failed={exc}")
        return None


def release_external_audio_defer(ready_path: Path, *, defer_file: Path | None = None) -> None:
    try:
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        ready_path.write_text(f"{time.time():.3f}\n", encoding="utf-8")
        if defer_file is not None:
            defer_file.unlink(missing_ok=True)
        print(f"[audio] external_audio_ready={ready_path}")
    except Exception as exc:
        print(f"[audio] external_audio_ready_failed={exc}")


def _external_audio_control_action(text: str) -> str | None:
    compact = _compact_text(text)
    if _EXTERNAL_AUDIO_STOP_CONTROL.search(compact):
        return "stop"
    if _EXTERNAL_AUDIO_PAUSE_CONTROL.search(compact):
        return "pause"
    if _EXTERNAL_AUDIO_RESUME_CONTROL.search(compact):
        return "resume"
    return None


def _is_external_audio_request(text: str) -> bool:
    compact = _compact_text(text)
    if _EXTERNAL_AUDIO_STOP_REQUEST.search(compact):
        return False
    return bool(_EXTERNAL_AUDIO_REQUEST.search(compact))


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


def _compact_text(text: str) -> str:
    return re.sub(r"[\s，。,.!?！？；;：:、]+", "", text)


def _text_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        texts = [str(text).strip() for text in value if str(text).strip()]
        return texts or list(default)
    return list(default)


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)
