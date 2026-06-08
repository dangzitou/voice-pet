from __future__ import annotations

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from ..audio.player import AudioPlayer
from ..asr.mimo_asr import MimoASR
from ..brain.direct_llm import DirectLLMAdapter
from ..brain.picoclaw import PicoBridgeConfig, PicoClawAdapter
from ..config import load_config
from ..runtime.actions import build_default_router
from ..runtime.state_machine import (
    VoicePetStateMachine,
    _format_spoken_reply,
    begin_external_audio_defer,
    release_external_audio_defer,
)
from ..runtime.wakeword import WakewordDetector
from ..tts.mimo_tts import MimoTTS


def main() -> None:
    parser = argparse.ArgumentParser(description="Run voice-pet MVP with mock TTS-generated input")
    parser.add_argument("--config", default="~/.picoclaw/voice-pet/config.json")
    parser.add_argument("--wake-text", default="小爱小爱")
    parser.add_argument("--user-text", default="小爱我今天心情不好")
    parser.add_argument("--play", action="store_true", help="play generated audio locally")
    parser.add_argument("--offline", action="store_true", help="run without MiMo or PicoClaw network calls")
    args = parser.parse_args()

    if args.offline:
        run_offline_mock(args.wake_text, args.user_text)
        return

    cfg = load_config(args.config)
    mimo = cfg["mimo"]
    audio = cfg["audio"]
    runtime = cfg["runtime"]
    wakeword_cfg = cfg["wakeword"]
    api_key = mimo.get("api_key", "").strip()
    if not api_key:
        raise ValueError("missing MIMO_API_KEY or config mimo.api_key")

    work_dir = Path(runtime["work_dir"]) / "mock-test"
    work_dir.mkdir(parents=True, exist_ok=True)
    timeout = int(runtime.get("request_timeout_seconds", 120))

    tts = MimoTTS(
        api_key,
        mimo["api_base"],
        mimo["tts_model"],
        mimo.get("tts_voice", "mimo_default"),
        mimo.get("tts_format", "wav"),
        timeout,
        mimo.get("tts_style_prompt", ""),
    )
    asr = MimoASR(api_key, mimo["api_base"], mimo["asr_model"], mimo.get("language", "zh"), timeout)
    brain_kind = str(runtime.get("brain", "picoclaw")).strip().lower()
    if brain_kind == "picoclaw":
        token = runtime.get("picoclaw_token", "").strip()
        if not token:
            raise ValueError("missing PICOCLAW_TOKEN or config runtime.picoclaw_token")
        brain = PicoClawAdapter(
            PicoBridgeConfig(
                url=runtime["picoclaw_ws_url"],
                token=token,
                session_id=runtime.get("picoclaw_session_id", "voice-pet"),
                timeout_seconds=float(runtime.get("request_timeout_seconds", 120)),
                node_script=runtime.get("picoclaw_node_script", "~/.picoclaw/voice-pet/pico_bridge_once.js"),
            )
        )
    elif brain_kind == "direct_llm":
        brain = DirectLLMAdapter(api_key, mimo["api_base"], mimo["llm_model"], timeout)
    else:
        raise ValueError(f"unsupported runtime.brain: {brain_kind}")
    detector = WakewordDetector(wakeword_cfg.get("aliases", []))
    router = build_default_router() if runtime.get("enable_local_actions", False) else None
    player = AudioPlayer(
        command=audio.get("playback_command", "aplay"),
        device=audio.get("playback_device", ""),
    )

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

    ack_audio_path = str(wakeword_cfg.get("ack_audio_path", "")).strip()
    if ack_audio_path and Path(ack_audio_path).expanduser().is_file():
        ack_audio = Path(ack_audio_path).expanduser()
        if args.play:
            player.play_file(str(ack_audio))
    else:
        ack_text = wakeword_cfg.get("ack_text", "主人，咋啦")
        ack_audio = work_dir / "ack.wav"
        ack_audio.write_bytes(tts.synthesize(ack_text))
        if args.play:
            player.play_file(str(ack_audio))

    user_audio = work_dir / "user.wav"
    user_audio.write_bytes(tts.synthesize(args.user_text))
    user_asr = asr.transcribe_file(str(user_audio)).strip()
    session_wake = detector.detect_prefix(user_asr)
    if not session_wake.matched:
        print(f"user_text: {args.user_text}")
        print(f"user_asr: {user_asr}")
        print("user_ignored: missing wakeword prefix")
        print(f"artifacts_dir: {work_dir}")
        return
    if not session_wake.cleaned_text:
        print(f"user_text: {args.user_text}")
        print(f"user_asr: {user_asr}")
        print("user_ignored: wakeword-only text")
        print(f"artifacts_dir: {work_dir}")
        return
    handled_text = session_wake.cleaned_text
    ready_path = begin_external_audio_defer(Path(runtime["work_dir"]), handled_text)
    try:
        local_reply = router.handle(handled_text) if router else None
        reply_text = local_reply or brain.reply(handled_text)
        reply_text = _format_spoken_reply(reply_text)

        reply_audio = work_dir / "reply.wav"
        reply_audio.write_bytes(tts.synthesize(reply_text))

        print(f"user_text: {args.user_text}")
        print(f"user_asr: {user_asr}")
        print(f"handled_text: {handled_text}")
        print(f"reply_text: {reply_text}")
        print(f"artifacts_dir: {work_dir}")

        if args.play:
            player.play_file(str(reply_audio))
    finally:
        if ready_path is not None:
            release_external_audio_defer(ready_path, defer_file=Path(runtime["work_dir"]) / "external-audio-defer.json")


def run_offline_mock(wake_text: str, user_text: str) -> None:
    with TemporaryDirectory() as tmp:
        ack_audio = Path(tmp) / "ack-prebuilt.wav"
        ack_audio.write_bytes(b"offline prebuilt ack")
        cfg = {
            "mimo": {
                "api_key": "offline",
                "api_base": "offline",
                "asr_model": "offline-asr",
                "tts_model": "offline-tts",
                "llm_model": "offline-llm",
                "language": "zh",
                "tts_voice": "offline",
                "tts_format": "wav",
                "tts_style_prompt": "",
            },
            "audio": {
                "sample_rate": 16000,
                "channels": 1,
                "record_device": "",
                "listen_mode": "streaming",
                "wake_max_seconds": 5.0,
                "wake_min_seconds": 0.4,
                "utterance_max_seconds": 8,
                "utterance_min_seconds": 1.0,
                "utterance_start_timeout_seconds": 8.0,
                "stream_chunk_ms": 100,
                "pre_roll_seconds": 0.3,
                "voice_start_threshold": 500,
                "silence_seconds": 1.2,
                "silence_threshold": 500,
            },
            "wakeword": {
                "aliases": ["小爱", "小艾", "小ai", "xiao ai", "xiaoai"],
                "max_extra_chars": 6,
                "cooldown_seconds": 0.0,
                "session_timeout_seconds": 60.0,
                "ack_text": "主人，咋啦",
                "ack_audio_path": str(ack_audio),
            },
            "runtime": {
                "work_dir": tmp,
                "request_timeout_seconds": 1,
                "poll_interval_seconds": 0.01,
                "brain": "picoclaw",
                "picoclaw_ws_url": "ws://127.0.0.1:18790/pico/ws",
                "picoclaw_token": "offline-token",
                "picoclaw_session_id": "voice-pet-offline",
                "picoclaw_manage_gateway": False,
                "enable_local_actions": False,
            },
        }

        machine = VoicePetStateMachine(cfg)
        detector = WakewordDetector(cfg["wakeword"]["aliases"])
        prefixed_user_text = user_text if detector.detect_prefix(user_text).matched else f"小爱{user_text}"
        expected_user_text = detector.detect_prefix(prefixed_user_text).cleaned_text
        ignored_user_text = "这句没有小爱前缀"

        fake_capture = _OfflineCapture(max_calls_before_timeout=3)
        fake_asr = _OfflineASR([wake_text, ignored_user_text, prefixed_user_text])
        fake_tts = _OfflineTTS()
        fake_brain = _OfflineBrain()
        fake_player = _OfflinePlayer()

        machine.capture = fake_capture
        machine.asr = fake_asr
        machine.tts = fake_tts
        machine.brain = fake_brain
        machine.player = fake_player

        machine.run_once()

        if fake_brain.requests != [expected_user_text]:
            raise AssertionError(f"brain requests = {fake_brain.requests!r}, want {[expected_user_text]!r}")
        expected_reply = f"mock picoclaw reply: {expected_user_text}。第二句完整保留。"
        if fake_tts.texts != [expected_reply]:
            raise AssertionError(f"tts texts = {fake_tts.texts!r}")
        if fake_player.paths[0] != str(ack_audio):
            raise AssertionError(f"ack player path = {fake_player.paths[0]!r}, want {str(ack_audio)!r}")
        if fake_capture.calls != 4:
            raise AssertionError(f"capture calls = {fake_capture.calls}, want 4")

        print("offline_mock: ok")
        print(f"wake_text: {wake_text}")
        print(f"ack_audio_path: {fake_player.paths[0]}")
        print(f"ignored_user_text: {ignored_user_text}")
        print(f"prefixed_user_text: {prefixed_user_text}")
        print(f"user_text: {fake_brain.requests[0]}")
        print(f"reply_text: {fake_tts.texts[0]}")


class _OfflineCapture:
    def __init__(self, max_calls_before_timeout: int = 2) -> None:
        self.calls = 0
        self.max_calls_before_timeout = max_calls_before_timeout

    def record_next_utterance(self, path: str, *args, **kwargs) -> str:
        self.calls += 1
        if self.calls > self.max_calls_before_timeout:
            raise TimeoutError("offline session timeout")
        Path(path).write_bytes(b"offline-audio")
        return path

    def record_for_duration(self, path: str, seconds: float) -> str:
        return self.record_next_utterance(path)


class _OfflineASR:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)

    def transcribe_file(self, path: str) -> str:
        if not self.texts:
            return ""
        return self.texts.pop(0)


class _OfflineTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        return f"offline audio: {text}".encode("utf-8")


class _OfflineBrain:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def reply(self, text: str) -> str:
        self.requests.append(text)
        return f"mock picoclaw reply: {text}。第二句完整保留。😂"


class _OfflinePlayer:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def play_file(self, path: str) -> None:
        self.paths.append(path)


if __name__ == "__main__":
    main()
