from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from voice_pet.runtime.state_machine import (
    VoicePetStateMachine,
    _external_audio_control_action,
    _format_spoken_reply,
    _split_spoken_chunks,
    begin_external_audio_defer,
    release_external_audio_defer,
)


class StateMachineTest(unittest.TestCase):
    def test_waiting_prompt_repeats_until_brain_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _SlowBrain(delay_seconds=0.16)
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.thinking_prompt_delay_seconds = 0.05
            machine.thinking_prompt_max_delay_seconds = 0.2
            machine.thinking_prompt_texts = ["稍等一下"]
            machine.thinking_prompt_paths = machine._prepare_thinking_prompt_paths()

            reply = machine._reply_with_waiting_prompt("今天有啥新闻")

            self.assertEqual(reply, "好了")
            self.assertEqual(len(machine.player.paths), 2)
            self.assertEqual(machine.tts.texts, ["稍等一下"])

    def test_waiting_prompt_uses_picoclaw_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _ProgressBrain(machine.brain_progress_path, delay_seconds=0.09)
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.thinking_prompt_delay_seconds = 0.05
            machine.thinking_prompt_max_delay_seconds = 0.2
            machine.thinking_prompt_texts = ["稍等一下"]
            machine.thinking_prompt_paths = machine._prepare_thinking_prompt_paths()

            reply = machine._reply_with_waiting_prompt("今天厦门天气咋样")

            self.assertEqual(reply, "好了")
            self.assertEqual(machine.tts.texts, ["我正在查询天气。"])
            self.assertEqual(len(machine.player.paths), 1)

    def test_waiting_prompt_deduplicates_picoclaw_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _ProgressBrain(machine.brain_progress_path, delay_seconds=0.22)
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.thinking_prompt_delay_seconds = 0.05
            machine.thinking_prompt_max_delay_seconds = 0.05
            machine.thinking_prompt_texts = ["稍等一下"]
            machine.thinking_prompt_paths = machine._prepare_thinking_prompt_paths()

            reply = machine._reply_with_waiting_prompt("查一下今天新闻")

            self.assertEqual(reply, "好了")
            self.assertEqual(machine.tts.texts, ["我正在查询天气。"])
            self.assertEqual(len(machine.player.paths), 1)

    def test_waiting_prompt_interval_increases_to_max(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.thinking_prompt_delay_seconds = 5.0
            machine.thinking_prompt_max_delay_seconds = 20.0

            intervals = [
                machine._thinking_prompt_interval_seconds(prompt_number)
                for prompt_number in range(1, 7)
            ]

            self.assertEqual(intervals, [5.0, 10.0, 15.0, 20.0, 20.0, 20.0])

    def test_zero_recording_max_seconds_uses_safe_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _base_config(tmp)
            config["audio"]["wake_max_seconds"] = 0
            config["audio"]["utterance_max_seconds"] = 0

            machine = VoicePetStateMachine(config)

            self.assertEqual(machine.wake_max_seconds, 5.0)
            self.assertEqual(machine.utterance_max_seconds, 20.0)

    def test_spoken_reply_cleanup_does_not_truncate_content(self) -> None:
        reply = "今天新闻主要有三条。第一，A 有新进展。第二，B 引发关注。第三，C 值得继续看。"

        self.assertEqual(_format_spoken_reply(reply), reply)

    def test_split_spoken_chunks_keeps_full_reply(self) -> None:
        reply = "今天新闻主要有三条。第一，A 有新进展。第二，B 引发关注。第三，C 值得继续看。"

        chunks = _split_spoken_chunks(reply, max_chars=18)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), reply)

    def test_say_reply_uses_chunked_tts_for_long_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.tts_chunk_max_chars = 16
            reply = "第一句先说。第二句继续说。第三句也要完整保留。"

            machine.say(reply, prefix="reply")

            self.assertEqual(machine.tts.texts, ["第一句先说。第二句继续说。", "第三句也要完整保留。"])
            self.assertEqual(len(machine.player.paths), 2)

    def test_external_audio_request_creates_ready_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            defer_file = work_dir / "external-audio-defer.json"

            ready_path = begin_external_audio_defer(
                work_dir,
                "播放邓紫棋的你把我灌醉",
                defer_file=defer_file,
            )

            self.assertIsNotNone(ready_path)
            assert ready_path is not None
            self.assertTrue(defer_file.is_file())

            release_external_audio_defer(ready_path, defer_file=defer_file)

            self.assertTrue(ready_path.is_file())
            self.assertFalse(defer_file.exists())

    def test_external_audio_stop_request_is_not_deferred(self) -> None:
        with TemporaryDirectory() as tmp:
            work_dir = Path(tmp)

            ready_path = begin_external_audio_defer(work_dir, "停止播放")

            self.assertIsNone(ready_path)
            self.assertFalse((work_dir / "external-audio-defer.json").exists())

    def test_external_audio_ignores_non_control_text(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            with patch.object(machine, "_external_audio_status", return_value="playing"):
                machine._handle_user_text("阿爸阿爸")

            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, [])
            self.assertEqual(machine.player.paths, [])

    def test_external_audio_control_pauses_without_tts(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="playing"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ), patch("voice_pet.runtime.state_machine.secrets.choice", lambda items: items[0]):
                machine._handle_user_text("暂停播放")

            self.assertEqual(calls, [["ncm-cli", "pause"]])
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(len(machine.tts.texts), 1)
            self.assertEqual(machine.player.paths, [str(machine.music_pause_prompt_paths[0])])

    def test_external_audio_control_stops_with_end_playback_wording(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="playing"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine._handle_user_text("结束播放")

            self.assertEqual(calls, [["ncm-cli", "stop"]])
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, [])

    def test_external_audio_new_music_request_is_forwarded_while_playing(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            with patch.object(machine, "_external_audio_status", return_value="playing"):
                machine._handle_user_text("播放一首周杰伦的歌")

            self.assertEqual(machine.brain.requests, ["播放一首周杰伦的歌"])
            self.assertEqual(machine.tts.texts, ["回复"])
            self.assertEqual(len(machine.player.paths), 1)

    def test_paused_external_audio_allows_conversation(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            with patch.object(machine, "_external_audio_status", return_value="paused"):
                machine._handle_user_text("今天厦门天气咋样")

            self.assertEqual(machine.brain.requests, ["今天厦门天气咋样"])
            self.assertEqual(machine.tts.texts, ["回复"])

    def test_paused_external_audio_wakeword_only_enters_session(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.capture = _LimitedCapture(max_calls=2)
            machine.asr = _OfflineASR(["小爱小爱", "小爱今天厦门天气咋样"])
            machine.wake_max_extra_chars = 1

            with patch.object(machine, "_external_audio_status", return_value="paused"):
                machine.run_once()

            self.assertEqual(machine.brain.requests, ["今天厦门天气咋样"])
            self.assertEqual(machine.tts.texts, ["主人，咋啦", "回复"])

    def test_external_audio_control_action_understands_stop_words(self) -> None:
        self.assertEqual(_external_audio_control_action("结束播放"), "stop")
        self.assertEqual(_external_audio_control_action("别播了"), "stop")

    def test_music_pause_control_from_idle_allows_extra_text(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.capture = _OneShotCapture()
            machine.asr = _OfflineASR(["小爱帮我暂停播放"])
            machine.wake_max_extra_chars = 1
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="playing"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ), patch("voice_pet.runtime.state_machine.secrets.choice", lambda items: items[0]):
                machine.run_once()

            self.assertEqual(calls, [["ncm-cli", "pause"]])
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(len(machine.tts.texts), 1)
            self.assertEqual(machine.player.paths, [str(machine.music_pause_prompt_paths[0])])


def _base_config(work_dir: str) -> dict:
    return {
        "mimo": {
            "api_key": "test",
            "api_base": "https://example.invalid/v1",
            "asr_model": "asr",
            "tts_model": "tts",
            "llm_model": "llm",
            "language": "zh",
            "tts_voice": "mimo_default",
            "tts_format": "wav",
            "tts_style_prompt": "",
        },
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
            "record_device": "",
            "playback_command": "aplay",
            "playback_device": "",
            "playback_cooldown_seconds": 0.0,
            "silence_threshold": 500,
            "silence_seconds": 1.2,
        },
        "wakeword": {
            "aliases": ["小爱"],
            "ack_text": "主人，咋啦",
            "thinking_prompt_delay_seconds": 0.05,
            "thinking_prompt_texts": ["稍等一下"],
        },
        "runtime": {
            "work_dir": work_dir,
            "request_timeout_seconds": 1,
            "brain": "picoclaw",
            "picoclaw_ws_url": "ws://127.0.0.1:18790/pico/ws",
            "picoclaw_token": "test-token",
            "picoclaw_manage_gateway": False,
        },
    }


class _SlowBrain:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds

    def reply(self, text: str) -> str:
        time.sleep(self.delay_seconds)
        return "好了"


class _ProgressBrain:
    def __init__(self, progress_path: Path, delay_seconds: float) -> None:
        self.progress_path = progress_path
        self.delay_seconds = delay_seconds

    def reply(self, text: str) -> str:
        self.progress_path.write_text(
            json.dumps({"text": "我正在查询天气", "updated_at": time.time()}),
            encoding="utf-8",
        )
        time.sleep(self.delay_seconds)
        return "好了"


class _FakeBrain:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def reply(self, text: str) -> str:
        self.requests.append(text)
        return "回复"


class _FakeTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        return b"wav"


class _FakePlayer:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def play_file(self, path: str) -> None:
        assert Path(path).is_file()
        self.paths.append(path)


class _OneShotCapture:
    def record_next_utterance(self, path: str, *args, **kwargs) -> str:
        Path(path).write_bytes(b"audio")
        return path

    def reset_stream(self) -> None:
        pass


class _LimitedCapture:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.calls = 0

    def record_next_utterance(self, path: str, *args, **kwargs) -> str:
        self.calls += 1
        if self.calls > self.max_calls:
            raise TimeoutError("no more speech")
        Path(path).write_bytes(b"audio")
        return path

    def reset_stream(self) -> None:
        pass


class _OfflineASR:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)

    def transcribe_file(self, path: str) -> str:
        return self.texts.pop(0)


class _Completed:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
