from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from voice_pet.runtime.state_machine import VoicePetStateMachine, _format_spoken_reply


class StateMachineTest(unittest.TestCase):
    def test_waiting_prompt_repeats_until_brain_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _SlowBrain(delay_seconds=0.16)
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.thinking_prompt_delay_seconds = 0.05
            machine.thinking_prompt_texts = ["稍等一下"]
            machine.thinking_prompt_paths = machine._prepare_thinking_prompt_paths()

            reply = machine._reply_with_waiting_prompt("今天有啥新闻")

            self.assertEqual(reply, "好了")
            self.assertEqual(len(machine.player.paths), 3)
            self.assertEqual(machine.tts.texts, ["稍等一下"])

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
