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

    def test_task_cancel_command_during_brain_wait_stops_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            brain = _CancellableBrain()
            machine.brain = brain
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.capture = _OneShotCapture()
            machine.asr = _OfflineASR(["小爱停下来先"])
            machine.thinking_prompt_delay_seconds = 0.05
            machine.thinking_prompt_max_delay_seconds = 0.05

            with patch.object(machine, "_external_audio_status", return_value="stopped"):
                machine._handle_user_text("帮我查一个很慢的任务")

            self.assertTrue(brain.cancel_seen)
            self.assertEqual(machine.tts.texts[-1], "好，先停下。")
            self.assertNotIn("不应该播出的慢任务回复", machine.tts.texts)
            self.assertGreaterEqual(len(machine.player.paths), 1)

    def test_music_task_cancel_does_not_release_deferred_playback(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            brain = _CancellableBrain()
            machine.brain = brain
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.capture = _OneShotCapture()
            machine.asr = _OfflineASR(["小爱停下来先"])
            machine.thinking_prompt_delay_seconds = 0.05
            machine.thinking_prompt_max_delay_seconds = 0.05
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd == ["ncm-cli", "state"]:
                    return _Completed(stdout='{"success":true,"state":{"status":"stopped"}}', stderr="", returncode=0)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch("voice_pet.runtime.state_machine.subprocess.run", fake_run):
                machine._handle_user_text("播放一首周杰伦的歌")

            self.assertTrue(brain.cancel_seen)
            self.assertEqual(machine.tts.texts[-1], "好，先停下。")
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())
            self.assertEqual(list(Path(tmp).glob("external-audio-ready-*.signal")), [])
            self.assertIn(["ncm-cli", "stop"], calls)

    def test_waiting_prompt_repeats_picoclaw_progress_by_interval(self) -> None:
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
            self.assertEqual(len(machine.player.paths), 4)

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

    def test_managed_gateway_env_uses_current_defer_file(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(
                "voice_pet.runtime.state_machine.os.environ",
                {"VOICE_PET_EXTERNAL_AUDIO_DEFER_FILE": "/tmp/stale-defer.json"},
            ):
                machine = VoicePetStateMachine(_base_config(tmp))

            self.assertIsNotNone(machine.gateway.env)
            assert machine.gateway.env is not None
            self.assertEqual(
                machine.gateway.env.get("VOICE_PET_EXTERNAL_AUDIO_DEFER_FILE"),
                str(Path(tmp) / "external-audio-defer.json"),
            )

    def test_spoken_reply_cleanup_does_not_truncate_content(self) -> None:
        reply = "今天新闻主要有三条。第一，A 有新进展。第二，B 引发关注。第三，C 值得继续看。"

        self.assertEqual(_format_spoken_reply(reply), reply)

    def test_spoken_reply_cleanup_removes_music_urls(self) -> None:
        reply = "好嘞，李荣浩《李白》已经在播了，链接在这里， https://music.163.com/song?id=27678655"

        self.assertEqual(_format_spoken_reply(reply), "好嘞，李荣浩《李白》已经在播了")

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

    def test_handle_user_text_skips_duplicate_progress_reply_but_releases_audio(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            def fake_reply(user_text: str) -> str:
                machine._played_progress_prompts.add("我正在处理音乐播放。")
                return "我正在处理音乐播放。"

            with patch.object(machine, "_reply_with_waiting_prompt", fake_reply):
                machine._handle_user_text("播放一首歌")

            self.assertEqual(machine.tts.texts, [])
            self.assertEqual(machine.player.paths, [])
            self.assertEqual(len(list(Path(tmp).glob("external-audio-ready-*.signal"))), 1)
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())

    def test_handle_user_text_skips_success_music_reply_without_progress_release(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain(reply='正在播放Aaron Smith的"Dancin"。')
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            with patch.object(machine, "_external_audio_status", return_value="stopped"):
                machine._handle_user_text("播放一首歌")

            self.assertEqual(machine.tts.texts, [])
            self.assertEqual(machine.player.paths, [])

    def test_handle_user_text_speaks_music_clarification_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain(reply="你想播放哪首歌？")
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            with patch.object(machine, "_external_audio_status", return_value="stopped"):
                machine._handle_user_text("播放一首歌")

            self.assertEqual(machine.tts.texts, ["你想播放哪首歌？"])
            self.assertEqual(len(machine.player.paths), 1)

    def test_handle_user_text_speaks_timer_reply_that_mentions_playing_music(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            def fake_reply(user_text: str) -> str:
                machine._external_audio_released_during_progress = True
                return "好，十分钟之后提醒你放邓紫棋的歌。"

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch.object(
                machine,
                "_reply_with_waiting_prompt",
                fake_reply,
            ):
                machine._handle_user_text("十分钟后提醒我播放邓紫棋的歌")

            self.assertEqual(machine.tts.texts, ["好，十分钟之后提醒你放邓紫棋的歌。"])
            self.assertEqual(len(machine.player.paths), 1)
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())
            self.assertEqual(list(Path(tmp).glob("external-audio-ready-*.signal")), [])

    def test_scheduled_music_request_stops_accidental_immediate_playback(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain("正在播放 G.E.M.邓紫棋 - 光年之外 在网易云音乐中打开。")
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run",
                fake_run,
            ):
                machine._handle_user_text("一分钟后播放光年之外")

            self.assertEqual(calls, [["ncm-cli", "stop"]])
            self.assertEqual(machine.tts.texts, ["好，我不会现在播放，到时间再提醒你。"])
            self.assertEqual(len(machine.player.paths), 1)
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())

    def test_music_progress_prompt_releases_external_audio_before_final_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            ready_path = begin_external_audio_defer(
                Path(tmp),
                "播放一首歌",
                defer_file=Path(tmp) / "external-audio-defer.json",
            )
            assert ready_path is not None
            machine._active_external_audio_ready_path = ready_path

            machine._play_progress_prompt("我正在处理音乐播放。")

            self.assertTrue(ready_path.is_file())
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())
            self.assertTrue(machine._external_audio_released_during_progress)
            self.assertIsNone(machine._active_external_audio_ready_path)
            self.assertEqual(machine.tts.texts, [])

    def test_handle_user_text_speaks_music_failure_after_audio_release_when_not_active(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            def fake_reply(user_text: str) -> str:
                machine._external_audio_released_during_progress = True
                return "抱歉，这首歌目前不可播放。"

            with patch.object(machine, "_reply_with_waiting_prompt", fake_reply), patch.object(
                machine,
                "_is_external_audio_active",
                return_value=False,
            ):
                machine._handle_user_text("播放一首歌")

            self.assertEqual(machine.tts.texts, ["抱歉，这首歌目前不可播放。"])
            self.assertEqual(len(machine.player.paths), 1)
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())

    def test_handle_user_text_skips_result_when_music_active_after_release(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            def fake_reply(user_text: str) -> str:
                machine._external_audio_released_during_progress = True
                return "正在播放。"

            with patch.object(machine, "_reply_with_waiting_prompt", fake_reply), patch.object(
                machine,
                "_is_external_audio_active",
                return_value=True,
            ):
                machine._handle_user_text("播放一首歌")

            self.assertEqual(machine.tts.texts, [])
            self.assertEqual(machine.player.paths, [])
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())

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

    def test_external_audio_defer_can_be_forced_for_resume(self) -> None:
        with TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            defer_file = work_dir / "external-audio-defer.json"

            ready_path = begin_external_audio_defer(
                work_dir,
                "继续播放",
                defer_file=defer_file,
                force=True,
            )

            self.assertIsNotNone(ready_path)
            self.assertTrue(defer_file.is_file())

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

    def test_external_audio_resume_control_does_not_fall_through_to_brain_when_stopped(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd == ["ncm-cli", "voice-pet-cache-status"]:
                    return _Completed(stdout='{"success":true}', stderr="", returncode=0)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine._handle_user_text("继续播放")

            self.assertEqual(calls, [["ncm-cli", "resume"]])
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, [])

    def test_external_audio_resume_uses_last_play_cache_when_state_is_stopped(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.music_last_play_path = Path(tmp) / "last-play.json"
            machine.music_last_play_path.write_text(
                json.dumps(
                    {
                        "encrypted_id": "encrypted-song-id",
                        "original_id": "27678655",
                        "name": "李白",
                        "artist_name": "李荣浩",
                        "updated_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            calls: list[list[str]] = []
            play_env = {}

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[0:2] == ["ncm-cli", "play"]:
                    play_env.update(kwargs.get("env") or {})
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine._handle_user_text("继续播放")

            self.assertEqual(calls[0], ["ncm-cli", "voice-pet-cache-status"])
            self.assertEqual(calls[1], [
                "ncm-cli",
                "play",
                "--song",
                "--encrypted-id",
                "encrypted-song-id",
                "--original-id",
                "27678655",
            ])
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, ["继续播放。"])
            self.assertEqual(len(machine.player.paths), 1)
            self.assertEqual(play_env.get("VOICE_PET_NCM_CACHE_ONLY"), "1")
            self.assertEqual(play_env.get("VOICE_PET_EXTERNAL_AUDIO_DEFER_FILE"), str(Path(tmp) / "external-audio-defer.json"))
            self.assertEqual(len(list(Path(tmp).glob("external-audio-ready-*.signal"))), 1)
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())

    def test_external_audio_new_music_request_is_forwarded_while_playing(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            with patch.object(machine, "_external_audio_status", return_value="playing"):
                machine._handle_user_text("播放一首周杰伦的歌")

            self.assertEqual(machine.brain.requests, ["播放一首周杰伦的歌"])
            self.assertEqual(machine.tts.texts, [])
            self.assertEqual(len(machine.player.paths), 0)

    def test_music_request_cache_hit_plays_without_brain(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            cache = {
                "version": 1,
                "entries": {
                    "播放李荣浩的李白": {
                        "text": "播放李荣浩的李白",
                        "key": "播放李荣浩的李白",
                        "encrypted_id": "encrypted-song-id",
                        "original_id": "27678655",
                        "name": "李白",
                        "artist_name": "李荣浩",
                        "updated_at": time.time(),
                    }
                },
            }
            machine.music_request_cache_path.write_text(json.dumps(cache), encoding="utf-8")
            calls: list[list[str]] = []
            play_env = {}

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[0:2] == ["ncm-cli", "play"]:
                    play_env.update(kwargs.get("env") or {})
                return _Completed(stdout='{"success":true,"message":"queued via mpv"}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine._handle_user_text("播放李荣浩的李白")

            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(calls[0], ["ncm-cli", "voice-pet-cache-status"])
            self.assertEqual(calls[1], [
                "ncm-cli",
                "play",
                "--song",
                "--encrypted-id",
                "encrypted-song-id",
                "--original-id",
                "27678655",
            ])
            self.assertEqual(machine.tts.texts, ["给你放上次那首，李荣浩的《李白》。"])
            self.assertEqual(len(machine.player.paths), 1)
            self.assertEqual(play_env.get("VOICE_PET_NCM_CACHE_ONLY"), "1")
            self.assertEqual(play_env.get("VOICE_PET_EXTERNAL_AUDIO_DEFER_FILE"), str(Path(tmp) / "external-audio-defer.json"))

    def test_music_request_cache_hit_ignores_trailing_modal_particle(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            cache = {
                "version": 1,
                "entries": {
                    "放首歌": {
                        "text": "放首歌",
                        "key": "放首歌",
                        "encrypted_id": "encrypted-song-id",
                        "original_id": "27678655",
                        "name": "光年之外",
                        "artist_name": "G.E.M.邓紫棋",
                        "updated_at": time.time(),
                    }
                },
            }
            machine.music_request_cache_path.write_text(json.dumps(cache), encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return _Completed(stdout='{"success":true,"message":"queued via mpv"}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine._handle_user_text("放首歌呗")

            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(calls[0], ["ncm-cli", "voice-pet-cache-status"])
            self.assertEqual(calls[1][0:2], ["ncm-cli", "play"])

    def test_music_request_cache_miss_falls_back_to_brain_and_remembers_last_play(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain("正在播放李荣浩的李白。")
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.music_last_play_path = Path(tmp) / "last-play.json"
            machine.music_last_play_path.write_text(
                json.dumps(
                    {
                        "encrypted_id": "encrypted-song-id",
                        "original_id": "27678655",
                        "name": "李白",
                        "artist_name": "李荣浩",
                        "updated_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(machine, "_external_audio_status", return_value="stopped"):
                machine._handle_user_text("播放李荣浩的李白")

            self.assertEqual(machine.brain.requests, ["播放李荣浩的李白"])
            payload = json.loads(machine.music_request_cache_path.read_text(encoding="utf-8"))
            entry = payload["entries"]["播放李荣浩的李白"]
            self.assertEqual(entry["encrypted_id"], "encrypted-song-id")
            self.assertEqual(entry["original_id"], "27678655")

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
        self.assertEqual(_external_audio_control_action("别播歌了"), "stop")
        self.assertEqual(_external_audio_control_action("停下来先"), "stop")
        self.assertIsNone(_external_audio_control_action("不要放弃"))

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

    def test_music_resume_control_from_idle_with_stopped_state_does_not_enter_brain(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.capture = _OneShotCapture()
            machine.asr = _OfflineASR(["小爱继续播放"])
            machine.wake_max_extra_chars = 1
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine.run_once()

            self.assertEqual(calls, [["ncm-cli", "resume"]])
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, [])

    def test_music_cache_ack_playback_failure_does_not_raise_or_block_ready_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FailingPlayer()
            cache = {
                "version": 1,
                "entries": {
                    "播放李荣浩的李白": {
                        "text": "播放李荣浩的李白",
                        "key": "播放李荣浩的李白",
                        "encrypted_id": "encrypted-song-id",
                        "original_id": "27678655",
                        "name": "李白",
                        "artist_name": "李荣浩",
                        "updated_at": time.time(),
                    }
                },
            }
            machine.music_request_cache_path.write_text(json.dumps(cache), encoding="utf-8")

            def fake_run(cmd, **kwargs):
                return _Completed(stdout='{"success":true,"message":"queued via mpv"}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine._handle_user_text("播放李荣浩的李白")

            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, ["给你放上次那首，李荣浩的《李白》。"])
            self.assertEqual(len(list(Path(tmp).glob("external-audio-ready-*.signal"))), 1)
            self.assertFalse((Path(tmp) / "external-audio-defer.json").exists())

    def test_stop_now_control_from_idle_with_stopped_state_does_not_enter_brain(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            machine.capture = _OneShotCapture()
            machine.asr = _OfflineASR(["小爱停下来先"])
            machine.wake_max_extra_chars = 1
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return _Completed(stdout='{"success":true}', stderr="", returncode=0)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine.run_once()

            self.assertEqual(calls, [["ncm-cli", "stop"]])
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, [])

    def test_external_audio_control_failure_is_logged_and_does_not_raise(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            def fake_run(cmd, **kwargs):
                return _Completed(stdout="", stderr="boom", returncode=2)

            with patch.object(machine, "_external_audio_status", return_value="playing"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                handled = machine._handle_external_audio_speech("停止播放", source="test", status="playing")

            self.assertTrue(handled)
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, [])

    def test_external_audio_control_os_error_is_handled_without_falling_through(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain()
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()

            def fake_run(cmd, **kwargs):
                raise OSError("missing ncm-cli")

            with patch.object(machine, "_external_audio_status", return_value="playing"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                handled = machine._handle_external_audio_speech("停止播放", source="test", status="playing")

            self.assertTrue(handled)
            self.assertEqual(machine.brain.requests, [])
            self.assertEqual(machine.tts.texts, [])

    def test_music_cache_play_failure_falls_back_to_brain_without_crashing(self) -> None:
        with TemporaryDirectory() as tmp:
            machine = VoicePetStateMachine(_base_config(tmp))
            machine.brain = _FakeBrain("换一首试试。")
            machine.tts = _FakeTTS()
            machine.player = _FakePlayer()
            cache = {
                "version": 1,
                "entries": {
                    "播放李荣浩的李白": {
                        "text": "播放李荣浩的李白",
                        "key": "播放李荣浩的李白",
                        "encrypted_id": "encrypted-song-id",
                        "original_id": "27678655",
                        "name": "李白",
                        "artist_name": "李荣浩",
                        "updated_at": time.time(),
                    }
                },
            }
            machine.music_request_cache_path.write_text(json.dumps(cache), encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd == ["ncm-cli", "voice-pet-cache-status"]:
                    return _Completed(stdout='{"success":true}', stderr="", returncode=0)
                return _Completed(stdout='{"success":false}', stderr="expired", returncode=2)

            with patch.object(machine, "_external_audio_status", return_value="stopped"), patch(
                "voice_pet.runtime.state_machine.subprocess.run", fake_run
            ):
                machine._handle_user_text("播放李荣浩的李白")

            self.assertEqual(machine.brain.requests, ["播放李荣浩的李白"])
            self.assertEqual(machine.tts.texts, ["换一首试试。"])
            payload = json.loads(machine.music_request_cache_path.read_text(encoding="utf-8"))
            self.assertNotIn("播放李荣浩的李白", payload["entries"])


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
        "music": {
            "last_play_path": str(Path(work_dir) / "last-play.json"),
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


class _CancellableBrain:
    def __init__(self) -> None:
        self.cancel_seen = False
        self.requests: list[str] = []

    def reply(self, text: str) -> str:
        self.requests.append(text)
        time.sleep(0.16)
        return "不应该播出的慢任务回复"

    def reply_with_cancel(self, text: str, cancel_event) -> str:
        self.requests.append(text)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                self.cancel_seen = True
                return "不应该播出的慢任务回复"
            time.sleep(0.01)
        return "不应该播出的慢任务回复"


class _FakeBrain:
    def __init__(self, reply: str = "回复") -> None:
        self.requests: list[str] = []
        self.response = reply

    def reply(self, text: str) -> str:
        self.requests.append(text)
        return self.response


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


class _FailingPlayer:
    def play_file(self, path: str) -> None:
        assert Path(path).is_file()
        raise RuntimeError("playback device busy")


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
