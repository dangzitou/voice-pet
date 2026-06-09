from __future__ import annotations

import io
import json
import hashlib
import os
import re
import secrets
import subprocess
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Event, Lock
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
        self._speech_lock = Lock()
        self.brain_progress_path = self.work_dir / "picoclaw-progress.json"
        self.external_audio_local_state_path = self.work_dir / "external-audio-local-state.json"
        self.music_request_cache_path = self.work_dir / "music-request-cache.json"
        last_play_path = str(music.get("last_play_path", "~/.config/ncm-cli/voice-pet-last-play.json")).strip()
        self.music_last_play_path = Path(last_play_path).expanduser()
        self._music_cache_bridge_supported: bool | None = None
        self._played_progress_prompts: set[str] = set()
        self._active_external_audio_ready_path: Path | None = None
        self._external_audio_released_during_progress = False
        self._task_cancel_requested = False
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
        self.tts_chunk_max_chars = _positive_int(mimo.get("tts_chunk_max_chars", 55), default=55)
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
                    node_script=runtime.get("picoclaw_node_script", "~/.picoclaw/voice-pet/pico_bridge_session.js"),
                    progress_path=str(self.brain_progress_path),
                ),
                on_push_message=self._handle_brain_push_message,
            )
        elif brain_kind == "direct_llm":
            self.brain = DirectLLMAdapter(api_key, mimo["api_base"], mimo["llm_model"], timeout)
        else:
            raise ValueError(f"unsupported runtime.brain: {brain_kind}")
        self.player = AudioPlayer(
            command=audio.get("playback_command", "aplay"),
            device=audio.get("playback_device", ""),
        )
        self.external_audio_defer_file = self.work_dir / "external-audio-defer.json"
        self.external_audio_defer_seconds = float(runtime.get("external_audio_defer_seconds", 180.0))
        self.gateway = PicoClawGatewayProcess(runtime, env=self._gateway_env())
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
        self.thinking_prompt_max_delay_seconds = float(wakeword.get("thinking_prompt_max_delay_seconds", 20.0))
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
        self.task_cancel_start_seconds = max(0.0, float(runtime.get("task_cancel_start_seconds", 0.5)))
        self.task_cancel_poll_seconds = _positive_float(runtime.get("task_cancel_poll_seconds", 0.5), default=0.5)
        self.task_cancel_record_timeout_seconds = _positive_float(
            runtime.get("task_cancel_record_timeout_seconds", 0.1),
            default=0.1,
        )
        self._last_wake_at = 0.0
        self._previous_idle_text = ""

    def _gateway_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["VOICE_PET_EXTERNAL_AUDIO_DEFER_FILE"] = str(self.external_audio_defer_file)
        return env

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
            close_brain = getattr(self.brain, "close", None)
            if callable(close_brain):
                close_brain()
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
        external_audio_status = self._external_audio_status()
        if _external_audio_can_control(external_audio_status):
            handled = self._handle_external_audio_speech(wake.cleaned_text, source="idle", status=external_audio_status)
            if handled or _external_audio_blocks_conversation(external_audio_status):
                return
            print(f"[wake] matched alias={wake.alias} with paused audio")
            self.play_ack()
            self._run_wake_session(initial_text=wake.cleaned_text)
            return
        if self._is_external_audio_active():
            self._handle_external_audio_speech(wake.cleaned_text, source="idle", status=external_audio_status)
            return
        if wake.cleaned_text and _is_explicit_external_audio_control(wake.cleaned_text):
            print(f"[wake] matched alias={wake.alias} command={wake.cleaned_text}")
            self._handle_user_text(wake.cleaned_text)
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
            external_audio_status = self._external_audio_status()
            if _external_audio_can_control(external_audio_status):
                handled = self._handle_external_audio_speech(
                    session_wake.cleaned_text,
                    source="session",
                    status=external_audio_status,
                )
                if handled or _external_audio_blocks_conversation(external_audio_status):
                    user_text = ""
                    deadline = time.monotonic() + self.session_timeout_seconds
                    continue
            if self._is_external_audio_active():
                self._handle_external_audio_speech(session_wake.cleaned_text, source="session", status=external_audio_status)
                user_text = ""
                deadline = time.monotonic() + self.session_timeout_seconds
                continue
            user_text = session_wake.cleaned_text

    def _handle_user_text(self, user_text: str) -> None:
        external_audio_status = self._external_audio_status()
        action = _external_audio_control_action(user_text)
        if action is not None and (
            _external_audio_can_control(external_audio_status)
            or _is_explicit_external_audio_control(user_text)
        ):
            self._handle_external_audio_speech(user_text, source="handler", status=external_audio_status)
            return
        if _external_audio_can_control(external_audio_status):
            if action is not None:
                self._handle_external_audio_speech(user_text, source="handler", status=external_audio_status)
                return
            if _external_audio_blocks_conversation(external_audio_status) and not _is_external_audio_request(user_text):
                self._handle_external_audio_speech(user_text, source="handler", status=external_audio_status)
                return
        print(f"[think] user={user_text}")
        if self._try_play_cached_music_request(user_text):
            return
        request_started_at = time.time()
        defer_ready_path = begin_external_audio_defer(
            self.work_dir,
            user_text,
            defer_file=self.external_audio_defer_file,
            timeout_seconds=self.external_audio_defer_seconds,
        )
        self._active_external_audio_ready_path = defer_ready_path
        self._external_audio_released_during_progress = False
        self._task_cancel_requested = False
        try:
            local_reply = self.router.handle(user_text) if self.router else None
            try:
                reply = local_reply or self._reply_with_waiting_prompt(user_text)
            except Exception as exc:
                if self._external_audio_released_during_progress:
                    print(f"[speak] reply_error_after_external_audio_release={exc}")
                    if _is_external_audio_request(user_text):
                        if self._is_external_audio_active():
                            print("[speak] skipped_error_after_music_release")
                        else:
                            self.say("主人，音乐这边没有返回结果，你可以换首歌试试。", prefix="reply")
                    elif not self._is_external_audio_active():
                        self.say("主人，音乐这边没有返回结果，你可以换首歌试试。", prefix="reply")
                    return
                raise
            reply = _format_spoken_reply(reply)
            if not reply:
                reply = "主人，我刚刚没组织好，再说一次吧。"
            if reply == TASK_CANCEL_REPLY:
                if _is_external_audio_request(user_text):
                    self._cancel_deferred_external_audio(defer_ready_path)
                print(f"[speak] task_cancel_reply={reply}")
                self.say(reply, prefix="reply")
                return
            if self._is_duplicate_progress_reply(reply):
                print(f"[speak] skipped_duplicate_progress_reply={reply}")
                return
            if _is_scheduled_reminder_request(user_text) and _is_external_audio_success_reply(reply):
                print(f"[music] scheduled_request_accidental_playback reply={reply}")
                self._stop_accidental_scheduled_music_playback()
                correction = "好，我不会现在播放，到时间再提醒你。"
                print(f"[speak] scheduled_music_correction={correction}")
                self.say(correction, prefix="reply")
                return
            if _is_external_audio_request(user_text) and self._should_skip_external_audio_reply(reply):
                print(f"[speak] skipped_external_audio_reply={reply}")
                return
            if _is_external_audio_request(user_text) and _is_external_audio_success_reply(reply):
                print(f"[speak] skipped_music_success_reply={reply}")
                return
            if self._external_audio_released_during_progress and _is_external_audio_request(user_text):
                if _is_external_audio_failure_reply(reply) and not self._is_external_audio_active():
                    print(f"[speak] music_failure_reply_after_audio_release={reply}")
                    self.say(reply, prefix="reply")
                    return
                print(f"[speak] skipped_music_reply_after_audio_release={reply}")
                return
            if self._external_audio_released_during_progress and self._is_external_audio_active():
                print(f"[speak] skipped_reply_while_external_audio_active={reply}")
                return
            print(f"[speak] reply={reply}")
            self.say(reply, prefix="reply")
        finally:
            if not self._task_cancel_requested:
                self._remember_music_request(user_text, request_started_at=request_started_at)
            self._played_progress_prompts.clear()
            if defer_ready_path is not None and self._active_external_audio_ready_path is not None:
                release_external_audio_defer(defer_ready_path, defer_file=self.external_audio_defer_file)
            self._active_external_audio_ready_path = None
            self._external_audio_released_during_progress = False
            self._task_cancel_requested = False

    def _is_external_audio_active(self) -> bool:
        status = self._external_audio_status()
        return _external_audio_blocks_conversation(status)

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
        status = str(state.get("status", "")).strip().lower()
        if status == "stopped":
            self._clear_external_audio_local_state()
            return status
        local_status = self._external_audio_local_status()
        if local_status and status in {"playing", "queued"}:
            return local_status
        return status

    def _handle_external_audio_speech(self, text: str, *, source: str, status: str = "") -> bool:
        status = status or self._external_audio_status()
        action = _external_audio_control_action(text)
        if action is None:
            if _external_audio_blocks_conversation(status) and _is_external_audio_request(text):
                print(f"[music] new_request while external audio active source={source} text={text}")
                self._handle_user_text(text)
                return True
            print(f"[music] ignored while external audio active source={source} text={text}")
            return _external_audio_blocks_conversation(status)

        print(f"[music] control source={source} action={action} text={text}")
        if action == "resume" and not _external_audio_can_control(status):
            if self._try_resume_last_external_audio():
                return True
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
            return True
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        print(f"[music] control_result action={action} code={result.returncode} stdout={stdout} stderr={stderr}")
        if result.returncode == 0 and action == "pause":
            self._set_external_audio_local_status("paused")
            self.play_music_pause_prompt()
        elif result.returncode == 0 and action in {"resume", "stop"}:
            self._clear_external_audio_local_state()
        return True

    def _cancel_deferred_external_audio(self, ready_path: Path | None) -> None:
        if ready_path is not None:
            try:
                ready_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"[audio] external_audio_ready_cancel_failed={exc}")
        try:
            self.external_audio_defer_file.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[audio] external_audio_defer_cancel_failed={exc}")
        self._active_external_audio_ready_path = None
        self._external_audio_released_during_progress = False
        try:
            result = subprocess.run(
                ["ncm-cli", "stop"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            print(
                "[music] cancel_result "
                f"code={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[music] cancel_failed error={exc}")

    def _stop_accidental_scheduled_music_playback(self) -> None:
        try:
            result = subprocess.run(
                ["ncm-cli", "stop"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            print(
                "[music] scheduled_stop_result "
                f"code={result.returncode} stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[music] scheduled_stop_failed error={exc}")

    def _try_resume_last_external_audio(self) -> bool:
        last_play = self._read_music_last_play()
        if not last_play:
            return False
        encrypted_id = str(last_play.get("encrypted_id", "")).strip()
        original_id = str(last_play.get("original_id", "")).strip()
        if not encrypted_id or not original_id:
            return False
        if not self._ncm_cache_bridge_supported():
            return False

        ready_path = begin_external_audio_defer(
            self.work_dir,
            "继续播放",
            defer_file=self.external_audio_defer_file,
            timeout_seconds=self.external_audio_defer_seconds,
            force=True,
        )
        env = os.environ.copy()
        env["VOICE_PET_NCM_CACHE_ONLY"] = "1"
        env["VOICE_PET_EXTERNAL_AUDIO_DEFER_FILE"] = str(self.external_audio_defer_file)
        try:
            result = self._time_call(
                "music_resume_last",
                subprocess.run,
                [
                    "ncm-cli",
                    "play",
                    "--song",
                    "--encrypted-id",
                    encrypted_id,
                    "--original-id",
                    original_id,
                ],
                capture_output=True,
                check=False,
                env=env,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[music] resume_last_failed error={exc}")
            if ready_path is not None:
                release_external_audio_defer(ready_path, defer_file=self.external_audio_defer_file)
            return False

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        print(f"[music] resume_last_result code={result.returncode} stdout={stdout} stderr={stderr}")
        if result.returncode != 0:
            if ready_path is not None:
                release_external_audio_defer(ready_path, defer_file=self.external_audio_defer_file)
            return False

        try:
            try:
                self.say("继续播放。", prefix="reply")
            except Exception as exc:
                print(f"[music] resume_last_ack_failed={exc}")
        finally:
            if ready_path is not None:
                release_external_audio_defer(ready_path, defer_file=self.external_audio_defer_file)
        return True

    def _try_play_cached_music_request(self, user_text: str) -> bool:
        if not _is_external_audio_request(user_text):
            return False
        entry = self._cached_music_entry(user_text)
        if not entry:
            return False
        encrypted_id = str(entry.get("encrypted_id", "")).strip()
        original_id = str(entry.get("original_id", "")).strip()
        if not encrypted_id or not original_id:
            return False
        if not self._ncm_cache_bridge_supported():
            return False

        print(
            "[music-cache] hit "
            f"key={_music_request_cache_key(user_text)} "
            f"name={entry.get('name', '')} artist={entry.get('artist_name', '')}"
        )
        ready_path = begin_external_audio_defer(
            self.work_dir,
            user_text,
            defer_file=self.external_audio_defer_file,
            timeout_seconds=self.external_audio_defer_seconds,
        )
        env = os.environ.copy()
        env["VOICE_PET_NCM_CACHE_ONLY"] = "1"
        env["VOICE_PET_EXTERNAL_AUDIO_DEFER_FILE"] = str(self.external_audio_defer_file)
        try:
            result = self._time_call(
                "music_cache_play",
                subprocess.run,
                [
                    "ncm-cli",
                    "play",
                    "--song",
                    "--encrypted-id",
                    encrypted_id,
                    "--original-id",
                    original_id,
                ],
                capture_output=True,
                check=False,
                env=env,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[music-cache] play_failed error={exc}")
            self._drop_music_request_cache(user_text)
            if ready_path is not None:
                release_external_audio_defer(ready_path, defer_file=self.external_audio_defer_file)
            return False

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        print(f"[music-cache] play_result code={result.returncode} stdout={stdout} stderr={stderr}")
        if result.returncode != 0:
            self._drop_music_request_cache(user_text)
            if ready_path is not None:
                release_external_audio_defer(ready_path, defer_file=self.external_audio_defer_file)
            return False

        try:
            try:
                self.say(_cached_music_ack_text(entry), prefix="reply")
            except Exception as exc:
                print(f"[music-cache] ack_failed={exc}")
        finally:
            if ready_path is not None:
                release_external_audio_defer(ready_path, defer_file=self.external_audio_defer_file)
        return True

    def _cached_music_entry(self, user_text: str) -> dict[str, Any] | None:
        key = _music_request_cache_key(user_text)
        if not key:
            return None
        payload = self._read_music_request_cache()
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return None
        entry = entries.get(key)
        if not isinstance(entry, dict):
            return None
        updated_at = _float_value(entry.get("updated_at"), 0.0)
        if updated_at and time.time() - updated_at > 7 * 24 * 60 * 60:
            self._drop_music_request_cache(user_text)
            return None
        return entry

    def _ncm_cache_bridge_supported(self) -> bool:
        if self._music_cache_bridge_supported is not None:
            return self._music_cache_bridge_supported
        try:
            result = subprocess.run(
                ["ncm-cli", "voice-pet-cache-status"],
                capture_output=True,
                check=False,
                text=True,
                timeout=1.5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[music-cache] bridge_check_failed={exc}")
            self._music_cache_bridge_supported = False
            return False
        self._music_cache_bridge_supported = result.returncode == 0
        if not self._music_cache_bridge_supported:
            print("[music-cache] bridge_not_supported")
        return self._music_cache_bridge_supported

    def _remember_music_request(self, user_text: str, *, request_started_at: float) -> None:
        if not _is_external_audio_request(user_text):
            return
        key = _music_request_cache_key(user_text)
        if not key:
            return
        last_play = self._read_music_last_play()
        if not last_play:
            return
        updated_at = _float_value(last_play.get("updated_at"), 0.0)
        if updated_at and updated_at < request_started_at - 2.0:
            print("[music-cache] last_play_stale")
            return
        encrypted_id = str(last_play.get("encrypted_id", "")).strip()
        original_id = str(last_play.get("original_id", "")).strip()
        if not encrypted_id or not original_id:
            return
        payload = self._read_music_request_cache()
        entries = payload.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            payload["entries"] = entries
        entry = {
            "text": user_text,
            "key": key,
            "encrypted_id": encrypted_id,
            "original_id": original_id,
            "name": str(last_play.get("name", "")).strip(),
            "artist_name": str(last_play.get("artist_name", "")).strip(),
            "updated_at": time.time(),
        }
        entries[key] = entry
        try:
            _write_json_atomic(self.music_request_cache_path, payload)
            print(
                "[music-cache] remembered "
                f"key={key} name={entry['name']} artist={entry['artist_name']}"
            )
        except OSError as exc:
            print(f"[music-cache] write_failed={exc}")

    def _drop_music_request_cache(self, user_text: str) -> None:
        key = _music_request_cache_key(user_text)
        if not key:
            return
        payload = self._read_music_request_cache()
        entries = payload.get("entries")
        if isinstance(entries, dict) and key in entries:
            entries.pop(key, None)
            try:
                _write_json_atomic(self.music_request_cache_path, payload)
            except OSError as exc:
                print(f"[music-cache] drop_failed={exc}")

    def _read_music_request_cache(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.music_request_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "entries": {}}
        if not isinstance(payload, dict):
            return {"version": 1, "entries": {}}
        payload.setdefault("version", 1)
        payload.setdefault("entries", {})
        return payload

    def _read_music_last_play(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.music_last_play_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _reply_with_waiting_prompt(self, user_text: str) -> str:
        self._clear_brain_progress()
        self._played_progress_prompts.clear()

        started_at = time.monotonic()
        cancel_event = Event()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._brain_reply, user_text, cancel_event)
        wait_for_reply = True
        try:
            prompt_count = 0
            prompt_interval_seconds = self._thinking_prompt_interval_seconds(prompt_count + 1)
            next_prompt_at = (
                started_at + prompt_interval_seconds
                if self.thinking_prompt_delay_seconds > 0 and self.thinking_prompt_texts
                else float("inf")
            )
            next_cancel_poll_at = started_at + self.task_cancel_start_seconds

            while True:
                timeout_seconds = max(0.0, min(next_prompt_at, next_cancel_poll_at) - time.monotonic())
                try:
                    reply = future.result(timeout=timeout_seconds)
                    elapsed_ms = (time.monotonic() - started_at) * 1000
                    print(
                        f"[timing] brain_reply={elapsed_ms:.0f}ms "
                        f"prompt_count={prompt_count}"
                    )
                    return reply
                except FutureTimeoutError:
                    pass

                now = time.monotonic()
                if now >= next_prompt_at:
                    prompt_count += 1
                    elapsed_ms = (now - started_at) * 1000
                    print(
                        f"[timing] brain_reply_waited={elapsed_ms:.0f}ms "
                        f"prompt_delay={prompt_interval_seconds:.1f}s "
                        f"prompt_count={prompt_count}"
                    )
                    if self._should_play_waiting_prompt(user_text):
                        self.play_thinking_prompt()
                    else:
                        print("[think] skipped_waiting_prompt external_audio_active")
                    prompt_interval_seconds = self._thinking_prompt_interval_seconds(prompt_count + 1)
                    next_prompt_at = time.monotonic() + prompt_interval_seconds

                if now >= next_cancel_poll_at:
                    if self._poll_task_cancel(cancel_event):
                        wait_for_reply = False
                        try:
                            future.result(timeout=0.2)
                        except FutureTimeoutError:
                            future.cancel()
                        except Exception as exc:
                            print(f"[task] cancel_worker_failed={exc}")
                        return TASK_CANCEL_REPLY
                    next_cancel_poll_at = time.monotonic() + self.task_cancel_poll_seconds
        finally:
            executor.shutdown(wait=wait_for_reply, cancel_futures=not wait_for_reply)
            self._clear_brain_progress()

    def _brain_reply(self, user_text: str, cancel_event: Event) -> str:
        reply_with_cancel = getattr(self.brain, "reply_with_cancel", None)
        if callable(reply_with_cancel):
            return reply_with_cancel(user_text, cancel_event)
        return self.brain.reply(user_text)

    def _poll_task_cancel(self, cancel_event: Event) -> bool:
        cancel_audio = self.work_dir / "task-cancel-candidate.wav"
        try:
            self._time_call(
                "task_cancel_record",
                self.capture.record_next_utterance,
                str(cancel_audio),
                max_seconds=min(self.utterance_max_seconds, 2.5),
                min_seconds=0.2,
                chunk_ms=self.stream_chunk_ms,
                pre_roll_seconds=self.pre_roll_seconds,
                start_timeout_seconds=self.task_cancel_record_timeout_seconds,
                start_threshold=self.voice_start_threshold,
                silence_threshold=self.silence_threshold,
                silence_seconds=min(self.utterance_silence_seconds, 0.8),
            )
        except TimeoutError:
            return False
        except Exception as exc:
            print(f"[task] cancel_listen_failed={exc}")
            return False

        try:
            self._reset_capture_stream()
            heard = self._time_call("task_cancel_asr", self.asr.transcribe_file, str(cancel_audio)).strip()
        except Exception as exc:
            print(f"[task] cancel_asr_failed={exc}")
            return False
        if not heard:
            return False
        wake = self.detector.detect_prefix(heard)
        if not wake.matched:
            print(f"[task] cancel_ignored missing_wake text={heard}")
            return False
        if not _is_task_cancel_control(wake.cleaned_text):
            print(f"[task] cancel_ignored text={wake.cleaned_text}")
            return False
        cancel_event.set()
        self._task_cancel_requested = True
        print(f"[task] cancel_requested text={wake.cleaned_text}")
        return True

    def _thinking_prompt_interval_seconds(self, prompt_number: int) -> float:
        base_delay = max(0.0, self.thinking_prompt_delay_seconds)
        max_delay = max(base_delay, self.thinking_prompt_max_delay_seconds)
        return min(base_delay * max(1, prompt_number), max_delay)

    def _should_play_waiting_prompt(self, user_text: str) -> bool:
        if not _is_external_audio_request(user_text):
            return True
        if self._is_external_audio_active():
            return False
        local_status = self._external_audio_local_status()
        if _external_audio_blocks_conversation(local_status):
            return False
        return True

    def _should_skip_external_audio_reply(self, reply: str) -> bool:
        if _is_external_audio_failure_reply(reply):
            return False
        if _is_external_audio_success_reply(reply):
            return True
        if self._is_external_audio_active():
            return True
        local_status = self._external_audio_local_status()
        if _external_audio_blocks_conversation(local_status):
            return True
        return False

    def play_thinking_prompt(self) -> None:
        progress_text = self._read_brain_progress_prompt()
        if progress_text:
            self._played_progress_prompts.add(progress_text)
            self._play_progress_prompt(progress_text)
            return
        prompt_path = self._pick_thinking_prompt_path()
        if prompt_path is None:
            return
        try:
            self._ensure_prompt_audio(prompt_path)
            print(f"[think] prompt_audio={prompt_path}")
            self._play_file_sync("think_prompt", prompt_path)
        except Exception as exc:
            print(f"[think] prompt_audio_failed={exc}")

    def _play_progress_prompt(self, text: str) -> None:
        audio_text = self._progress_prompt_audio_text(text)
        prompt_path = self._progress_prompt_path(audio_text)
        is_external_audio_prompt = self._is_external_audio_progress_prompt(text)
        try:
            if is_external_audio_prompt and self._is_external_audio_active():
                print("[think] skipped_progress_prompt external_audio_active")
                return
            if is_external_audio_prompt and _wav_file_duration_seconds(prompt_path) > 2.8:
                prompt_path.unlink(missing_ok=True)
            if not prompt_path.is_file():
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                audio_bytes = self._time_call("progress_prompt_tts", self.tts.synthesize, audio_text)
                if is_external_audio_prompt:
                    audio_bytes = _limit_wav_duration(audio_bytes, max_seconds=2.8)
                prompt_path.write_bytes(audio_bytes)
            print(f"[think] progress_prompt={text} audio_text={audio_text} audio={prompt_path}")
            self._play_file_sync("progress_prompt", prompt_path)
        except Exception as exc:
            print(f"[think] progress_prompt_failed={exc}")
        finally:
            if is_external_audio_prompt:
                self._release_external_audio_after_progress_prompt()

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

    def _progress_prompt_path(self, text: str) -> Path:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        return self.work_dir / "progress-prompts" / f"progress-{digest}.wav"

    def _read_brain_progress_prompt(self) -> str:
        try:
            payload = json.loads(self.brain_progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        updated_at = _float_value(payload.get("updated_at"), 0.0)
        if updated_at and time.time() - updated_at > 180.0:
            return ""
        text = _format_progress_prompt(str(payload.get("text", "")))
        return text

    def _is_duplicate_progress_reply(self, reply: str) -> bool:
        reply_compact = _compact_text(reply)
        if not reply_compact:
            return False
        for progress_text in self._played_progress_prompts:
            progress_compact = _compact_text(progress_text)
            if not progress_compact:
                continue
            if reply_compact == progress_compact:
                return True
            if reply_compact.startswith(progress_compact):
                suffix = reply_compact[len(progress_compact):]
                if suffix in _DUPLICATE_PROGRESS_REPLY_SUFFIXES:
                    return True
        return False

    def _is_external_audio_progress_prompt(self, text: str) -> bool:
        compact = _compact_text(text)
        return "音乐" in compact or "播放" in compact

    def _progress_prompt_audio_text(self, text: str) -> str:
        if self._is_external_audio_progress_prompt(text):
            return "正在准备播放。"
        return text

    def _release_external_audio_after_progress_prompt(self) -> None:
        ready_path = self._active_external_audio_ready_path
        if ready_path is None:
            return
        release_external_audio_defer(ready_path, defer_file=self.external_audio_defer_file)
        self._active_external_audio_ready_path = None
        self._external_audio_released_during_progress = True

    def _clear_brain_progress(self) -> None:
        try:
            self.brain_progress_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _external_audio_local_status(self) -> str:
        try:
            payload = json.loads(self.external_audio_local_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        updated_at = _float_value(payload.get("updated_at"), 0.0)
        if updated_at and time.time() - updated_at > 24 * 60 * 60:
            self._clear_external_audio_local_state()
            return ""
        return str(payload.get("status", "")).strip().lower()

    def _set_external_audio_local_status(self, status: str) -> None:
        try:
            _write_json_atomic(
                self.external_audio_local_state_path,
                {"status": status, "updated_at": time.time()},
            )
        except OSError as exc:
            print(f"[music] local_state_write_failed={exc}")

    def _clear_external_audio_local_state(self) -> None:
        try:
            self.external_audio_local_state_path.unlink(missing_ok=True)
        except OSError:
            pass

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
        with self._speech_lock:
            chunks = _split_spoken_chunks(text, max_chars=self.tts_chunk_max_chars)
            if prefix == "reply" and len(chunks) > 1:
                self._say_chunks(chunks, prefix)
                return
            audio_bytes = self._time_call(f"{prefix}_tts", self.tts.synthesize, text)
            audio_path = self.work_dir / f"{prefix}.wav"
            audio_path.write_bytes(audio_bytes)
            self._play_file_sync(prefix, audio_path)

    def _handle_brain_push_message(self, text: str) -> None:
        spoken = _format_spoken_reply(text)
        if not spoken:
            return
        print(f"[picoclaw] push_message={spoken}")
        try:
            self.say(spoken, prefix="push")
        except Exception as exc:
            print(f"[picoclaw] push_message_failed={exc}")

    def _say_chunks(self, chunks: list[str], prefix: str) -> None:
        chunk_dir = self.work_dir / f"{prefix}-chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        print(f"[speak] chunked_tts chunks={len(chunks)}")
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                self._synthesize_chunk_to_file,
                chunks[0],
                chunk_dir / f"{prefix}-001.wav",
                f"{prefix}_chunk_001",
            )
            first_path = first_future.result()
            rest_futures = [
                executor.submit(
                    self._synthesize_chunk_to_file,
                    chunk,
                    chunk_dir / f"{prefix}-{index:03d}.wav",
                    f"{prefix}_chunk_{index:03d}",
                )
                for index, chunk in enumerate(chunks[1:], start=2)
            ]
            self._play_file_sync(f"{prefix}_chunk_001", first_path, cooldown=not rest_futures)
            for index, future in enumerate(rest_futures, start=2):
                chunk_path = future.result()
                self._play_file_sync(
                    f"{prefix}_chunk_{index:03d}",
                    chunk_path,
                    cooldown=index == len(chunks),
                )

    def _synthesize_chunk_to_file(self, text: str, path: Path, label: str) -> Path:
        audio_bytes = self._time_call(f"{label}_tts", self.tts.synthesize, text)
        path.write_bytes(audio_bytes)
        return path

    def _play_file_sync(self, label: str, path: Path, *, cooldown: bool = True) -> None:
        self._reset_capture_stream()
        self._time_call(f"{label}_play", self.player.play_file, str(path))
        if cooldown and self.playback_cooldown_seconds > 0:
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
_SPOKEN_SENTENCE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?")
_SPOKEN_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_LINK_PHRASE = re.compile(r"[，,。；;\s]*(?:链接(?:在这里|是|如下)?|地址(?:在这里|是|如下)?)[，,：:\s]*")
_EXTERNAL_AUDIO_REQUEST = re.compile(r"(?:播放|放首|放一首|点歌|点一首|来一首|听歌|听音乐|播歌|放歌)")
_SCHEDULED_REMINDER_REQUEST = re.compile(r"(?:提醒|定时|闹钟|倒计时|稍后|过(?:一会|会儿|下)|之后|以后|分钟后|小时后|秒钟后|天后)")
_EXTERNAL_AUDIO_STOP_REQUEST = re.compile(r"(?:停止播放|结束播放|暂停播放|关闭音乐|关掉音乐|停止音乐|结束音乐|暂停音乐|停歌|别放歌|别播歌|别放音乐|别播音乐|不要放歌|不要播歌|不要放音乐|不要播音乐)")
_EXTERNAL_AUDIO_STOP_CONTROL = re.compile(r"(?:停止播放|结束播放|关闭播放|关掉播放|停止音乐|结束音乐|关闭音乐|关掉音乐|停歌|停下来|先停下来|停下|先停下|停止|结束|关闭|关掉|别放歌|别播歌|别放音乐|别播音乐|不要放歌|不要播歌|不要放音乐|不要播音乐)")
_EXTERNAL_AUDIO_PAUSE_CONTROL = re.compile(r"(?:暂停播放|暂停音乐|暂停|先停一下|等一下)")
_EXTERNAL_AUDIO_RESUME_CONTROL = re.compile(r"(?:继续播放|恢复播放|继续音乐|恢复音乐|继续|恢复|接着放)")
_EXTERNAL_AUDIO_CONTROL_CONTEXT = re.compile(r"(?:播放|音乐|歌|播|放)")
_EXTERNAL_AUDIO_FAILURE_REPLY = re.compile(r"(?:失败|找不到|没找到|无法播放|不能播放|不可播放|没有返回|出错|报错|换首歌|换一首)")
_EXTERNAL_AUDIO_SUCCESS_REPLY = re.compile(r"(?:正在播放|已经(?:在播|播放|开始播放)|给你(?:播放|放)|开始播放)")
_TASK_CANCEL_CONTROL = re.compile(r"(?:停下来先|先停下来|停下先|先停下|停下来|停下|停止任务|停止执行|取消任务|取消执行|别做了|不要做了)")
_DUPLICATE_PROGRESS_REPLY_SUFFIXES = {"", "了", "啦", "中", "中啦", "请稍等", "稍等一下"}
TASK_CANCEL_REPLY = "好，先停下。"
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


def _split_spoken_chunks(text: str, *, max_chars: int = 55) -> list[str]:
    spoken = _format_spoken_reply(text)
    if not spoken:
        return []

    chunks: list[str] = []
    current = ""
    for match in _SPOKEN_SENTENCE.finditer(spoken):
        sentence = match.group(0).strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_spoken_sentence(sentence, max_chars=max_chars))
            continue
        if current and len(current) + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return chunks or [spoken]


def _split_long_spoken_sentence(sentence: str, *, max_chars: int) -> list[str]:
    parts = [part for part in re.split(r"([，,、：:])", sentence) if part]
    chunks: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) > max_chars:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)

    result: list[str] = []
    for chunk in chunks:
        while len(chunk) > max_chars:
            result.append(chunk[:max_chars])
            chunk = chunk[max_chars:]
        if chunk:
            result.append(chunk)
    return result


def begin_external_audio_defer(
    work_dir: Path,
    user_text: str,
    *,
    defer_file: Path | None = None,
    timeout_seconds: float = 180.0,
    force: bool = False,
) -> Path | None:
    if not force and not _is_external_audio_request(user_text):
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


def _is_explicit_external_audio_control(text: str) -> bool:
    compact = _compact_text(text)
    action = _external_audio_control_action(compact)
    if action is None:
        return False
    if _EXTERNAL_AUDIO_CONTROL_CONTEXT.search(compact):
        return True
    if action == "resume" and compact in {"继续", "恢复", "接着放"}:
        return True
    if action == "pause" and compact in {"暂停", "先停一下", "等一下"}:
        return True
    if action == "stop" and compact in {
        "停止",
        "结束",
        "关闭",
        "关掉",
        "停下来",
        "停下来先",
        "先停下来",
        "停下",
        "停下先",
        "先停下",
        "别放",
        "别播",
        "不要放",
        "不要播",
    }:
        return True
    return False


def _external_audio_can_control(status: str) -> bool:
    return status in {"playing", "queued", "paused"}


def _external_audio_blocks_conversation(status: str) -> bool:
    return status in {"playing", "queued"}


def _is_external_audio_request(text: str) -> bool:
    compact = _compact_text(text)
    if _EXTERNAL_AUDIO_STOP_REQUEST.search(compact):
        return False
    if _is_scheduled_reminder_request(compact):
        return False
    return bool(_EXTERNAL_AUDIO_REQUEST.search(compact))


def _is_scheduled_reminder_request(text: str) -> bool:
    return bool(_SCHEDULED_REMINDER_REQUEST.search(_compact_text(text)))


def _is_external_audio_failure_reply(text: str) -> bool:
    return bool(_EXTERNAL_AUDIO_FAILURE_REPLY.search(_compact_text(text)))


def _is_external_audio_success_reply(text: str) -> bool:
    compact = _compact_text(text)
    if "?" in text or "？" in text:
        return False
    return bool(_EXTERNAL_AUDIO_SUCCESS_REPLY.search(compact)) and not _is_external_audio_failure_reply(compact)


def _is_task_cancel_control(text: str) -> bool:
    compact = _compact_text(text)
    if _EXTERNAL_AUDIO_CONTROL_CONTEXT.search(compact):
        return False
    return bool(_TASK_CANCEL_CONTROL.search(compact))


def _format_progress_prompt(text: str) -> str:
    spoken = _clean_spoken_text(text)
    if not spoken:
        return ""
    if not spoken.startswith(("我正在", "主人，我正在")):
        spoken = f"我正在{spoken}"
    spoken = spoken[:36].rstrip("，,。.")
    return f"{spoken}。"


def _positive_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _float_value(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_spoken_text(text: str) -> str:
    had_url = bool(_SPOKEN_URL.search(text))
    text = _SPOKEN_URL.sub("", text)
    text = _LINK_PHRASE.sub("，", text)
    text = _EMOJI.sub("", text)
    text = _MARKDOWN_NOISE.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _SPACES.sub(" ", text).strip()
    text = _LEADING_FILLERS.sub("", text).strip()
    if had_url:
        text = re.sub(r"[，,；;：:\s]+$", "", text)
    return text.strip()


def _compact_text(text: str) -> str:
    return re.sub(r"[\s，。,.!?！？；;：:、]+", "", text)


def _music_request_cache_key(text: str) -> str:
    compact = _compact_text(text)
    compact = re.sub(r"^(?:小爱|小艾|小ai|xiaoai)+", "", compact, flags=re.IGNORECASE)
    compact = re.sub(r"(?:呗|吧|啦|啊|呀|呢|哈)+$", "", compact)
    return compact.lower()


def _cached_music_ack_text(entry: dict[str, Any]) -> str:
    name = str(entry.get("name", "")).strip()
    artist = str(entry.get("artist_name", "")).strip()
    if name and artist:
        return f"给你放上次那首，{artist}的《{name}》。"
    if name:
        return f"给你放上次那首，《{name}》。"
    return "给你放上次那首。"


def _text_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        texts = [str(text).strip() for text in value if str(text).strip()]
        return texts or list(default)
    return list(default)


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def _wav_file_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio_file:
            frame_rate = audio_file.getframerate()
            if frame_rate <= 0:
                return 0.0
            return audio_file.getnframes() / frame_rate
    except (OSError, wave.Error, EOFError):
        return 0.0


def _limit_wav_duration(audio_bytes: bytes, *, max_seconds: float) -> bytes:
    if max_seconds <= 0:
        return audio_bytes
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as source:
            params = source.getparams()
            max_frames = int(params.framerate * max_seconds)
            if max_frames <= 0 or source.getnframes() <= max_frames:
                return audio_bytes
            frames = source.readframes(max_frames)
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setparams(params._replace(nframes=0))
            target.writeframes(frames)
        return output.getvalue()
    except (wave.Error, EOFError, OSError):
        return audio_bytes
