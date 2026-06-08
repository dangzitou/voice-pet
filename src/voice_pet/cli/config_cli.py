from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "~/.picoclaw/voice-pet/config.json"


MODEL_FIELDS = {
    "api_key": "mimo.api_key",
    "api_base": "mimo.api_base",
    "asr_model": "mimo.asr_model",
    "tts_model": "mimo.tts_model",
    "llm_model": "mimo.llm_model",
    "language": "mimo.language",
    "tts_voice": "mimo.tts_voice",
    "tts_format": "mimo.tts_format",
    "tts_style_prompt": "mimo.tts_style_prompt",
}


RUNTIME_FIELDS = {
    "brain": "runtime.brain",
    "picoclaw_ws_url": "runtime.picoclaw_ws_url",
    "picoclaw_session_id": "runtime.picoclaw_session_id",
    "picoclaw_node_script": "runtime.picoclaw_node_script",
    "enable_local_actions": "runtime.enable_local_actions",
    "picoclaw_manage_gateway": "runtime.picoclaw_manage_gateway",
    "picoclaw_gateway_command": "runtime.picoclaw_gateway_command",
    "picoclaw_gateway_args": "runtime.picoclaw_gateway_args",
    "picoclaw_gateway_cwd": "runtime.picoclaw_gateway_cwd",
    "picoclaw_gateway_ready_url": "runtime.picoclaw_gateway_ready_url",
    "picoclaw_gateway_start_timeout_seconds": "runtime.picoclaw_gateway_start_timeout_seconds",
    "picoclaw_gateway_stop_timeout_seconds": "runtime.picoclaw_gateway_stop_timeout_seconds",
}


AUDIO_FIELDS = {
    "record_device": "audio.record_device",
    "voice_start_threshold": "audio.voice_start_threshold",
    "silence_threshold": "audio.silence_threshold",
    "silence_seconds": "audio.silence_seconds",
    "wake_silence_seconds": "audio.wake_silence_seconds",
    "utterance_silence_seconds": "audio.utterance_silence_seconds",
    "wake_max_seconds": "audio.wake_max_seconds",
    "utterance_max_seconds": "audio.utterance_max_seconds",
    "playback_command": "audio.playback_command",
    "playback_device": "audio.playback_device",
    "playback_cooldown_seconds": "audio.playback_cooldown_seconds",
}


WAKEWORD_FIELDS = {
    "ack_text": "wakeword.ack_text",
    "ack_audio_path": "wakeword.ack_audio_path",
    "ack_texts": "wakeword.ack_texts",
    "ack_audio_paths": "wakeword.ack_audio_paths",
    "thinking_prompt_delay_seconds": "wakeword.thinking_prompt_delay_seconds",
    "thinking_prompt_texts": "wakeword.thinking_prompt_texts",
    "max_extra_chars": "wakeword.max_extra_chars",
    "session_timeout_seconds": "wakeword.session_timeout_seconds",
}

CONFIG_LABELS = MODEL_FIELDS | AUDIO_FIELDS | WAKEWORD_FIELDS | RUNTIME_FIELDS


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure voice-pet")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"config file path, default: {DEFAULT_CONFIG_PATH}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a config file from config.example.json")
    _add_subcommand_config_arg(init_parser)
    init_parser.add_argument(
        "--example",
        default="",
        help="example config path, default: ./config.example.json or repository config.example.json",
    )
    init_parser.add_argument("--force", action="store_true", help="overwrite an existing config file")
    init_parser.set_defaults(func=_cmd_init)

    show_parser = subparsers.add_parser("show", help="show current model configuration")
    _add_subcommand_config_arg(show_parser)
    show_parser.add_argument("--json", action="store_true", help="print model configuration as JSON")
    show_parser.set_defaults(func=_cmd_show)

    set_parser = subparsers.add_parser("set", help="update model configuration")
    _add_subcommand_config_arg(set_parser)
    api_key = set_parser.add_mutually_exclusive_group()
    api_key.add_argument("--api-key", dest="api_key", help="MiMo API key saved in config.json")
    api_key.add_argument(
        "--clear-api-key",
        dest="clear_api_key",
        action="store_true",
        help="remove the MiMo API key from config.json",
    )
    set_parser.add_argument("--api-base", "--base-url", "--baseurl", dest="api_base", help="MiMo API endpoint")
    set_parser.add_argument("--asr-model", "--asr-model-name", dest="asr_model", help="ASR model name")
    set_parser.add_argument("--tts-model", "--tts-model-name", dest="tts_model", help="TTS model name")
    set_parser.add_argument("--model", "--llm-model", "--model-name", dest="llm_model", help="text reply model name")
    set_parser.add_argument("--language", dest="language", help="ASR language, for example: zh")
    set_parser.add_argument("--tts-voice", dest="tts_voice", help="TTS voice name")
    set_parser.add_argument("--tts-format", dest="tts_format", help="TTS audio format, for example: wav")
    set_parser.add_argument("--tts-style-prompt", dest="tts_style_prompt", help="TTS style prompt, for example: cute, youthful, natural Chinese delivery")
    set_parser.add_argument("--record-device", dest="record_device", help="ALSA capture device passed to arecord -D")
    set_parser.add_argument("--voice-start-threshold", dest="voice_start_threshold", type=int, help="RMS threshold to start recording")
    set_parser.add_argument("--silence-threshold", dest="silence_threshold", type=int, help="RMS threshold treated as silence")
    set_parser.add_argument("--silence-seconds", dest="silence_seconds", type=float, help="seconds of silence to end recording")
    set_parser.add_argument(
        "--wake-silence-seconds",
        dest="wake_silence_seconds",
        type=float,
        help="seconds of silence to end a wakeword candidate",
    )
    set_parser.add_argument(
        "--utterance-silence-seconds",
        dest="utterance_silence_seconds",
        type=float,
        help="seconds of silence to end a user utterance after wake",
    )
    set_parser.add_argument("--wake-max-seconds", dest="wake_max_seconds", type=float, help="maximum seconds for a wake candidate")
    set_parser.add_argument("--utterance-max-seconds", dest="utterance_max_seconds", type=float, help="maximum seconds for a user utterance after wake")
    set_parser.add_argument("--playback-command", dest="playback_command", help="audio playback command")
    set_parser.add_argument("--playback-device", dest="playback_device", help="ALSA playback device passed to aplay -D")
    set_parser.add_argument(
        "--playback-cooldown",
        dest="playback_cooldown_seconds",
        type=float,
        help="seconds to wait after each playback before recording again",
    )
    set_parser.add_argument("--ack-text", dest="ack_text", help="wake acknowledgement text")
    set_parser.add_argument(
        "--ack-audio-path",
        dest="ack_audio_path",
        help="prebuilt wake acknowledgement audio file; falls back to ack text TTS if missing",
    )
    set_parser.add_argument(
        "--ack-text-variant",
        dest="ack_texts",
        action="append",
        help="append a wake acknowledgement text variant; repeat this flag to add multiple variants",
    )
    set_parser.add_argument(
        "--ack-audio-variant",
        dest="ack_audio_paths",
        action="append",
        help="append a wake acknowledgement audio path variant; repeat this flag to add multiple variants",
    )
    set_parser.add_argument(
        "--thinking-prompt-delay",
        dest="thinking_prompt_delay_seconds",
        type=float,
        help="seconds to wait before playing a random prebuilt thinking prompt while the agent is still replying",
    )
    set_parser.add_argument(
        "--thinking-prompt-text",
        dest="thinking_prompt_texts",
        action="append",
        help="append a prebuilt thinking prompt text; repeat this flag to add multiple prompts",
    )
    set_parser.add_argument(
        "--wake-max-extra-chars",
        dest="max_extra_chars",
        type=int,
        help="ignore wake matches when ASR text has more extra characters than this; -1 disables the filter",
    )
    set_parser.add_argument(
        "--wake-session-timeout",
        dest="session_timeout_seconds",
        type=float,
        help="seconds to stay in wake mode without new user speech",
    )
    set_parser.add_argument(
        "--brain",
        choices=("picoclaw", "direct_llm"),
        help="reply backend; picoclaw is the normal runtime core, direct_llm is for debugging",
    )
    set_parser.add_argument("--picoclaw-ws-url", dest="picoclaw_ws_url", help="PicoClaw gateway WebSocket URL")
    set_parser.add_argument("--picoclaw-session-id", dest="picoclaw_session_id", help="PicoClaw session id")
    set_parser.add_argument(
        "--picoclaw-node-script",
        dest="picoclaw_node_script",
        help="path to pico_bridge_once.js",
    )
    gateway_management = set_parser.add_mutually_exclusive_group()
    gateway_management.add_argument(
        "--manage-picoclaw-gateway",
        dest="picoclaw_manage_gateway",
        action="store_true",
        default=None,
        help="start and stop PicoClaw gateway from voice-pet",
    )
    gateway_management.add_argument(
        "--no-manage-picoclaw-gateway",
        dest="picoclaw_manage_gateway",
        action="store_false",
        help="connect to an already-running PicoClaw gateway",
    )
    set_parser.add_argument("--picoclaw-gateway-command", dest="picoclaw_gateway_command", help="PicoClaw command")
    set_parser.add_argument(
        "--picoclaw-gateway-args",
        dest="picoclaw_gateway_args",
        help='PicoClaw gateway command args, for example: "gateway --debug"',
    )
    set_parser.add_argument("--picoclaw-gateway-cwd", dest="picoclaw_gateway_cwd", help="PicoClaw gateway working directory")
    set_parser.add_argument("--picoclaw-gateway-ready-url", dest="picoclaw_gateway_ready_url", help="PicoClaw /ready URL")
    set_parser.add_argument(
        "--picoclaw-gateway-start-timeout",
        dest="picoclaw_gateway_start_timeout_seconds",
        type=float,
        help="seconds to wait for managed gateway startup",
    )
    set_parser.add_argument(
        "--picoclaw-gateway-stop-timeout",
        dest="picoclaw_gateway_stop_timeout_seconds",
        type=float,
        help="seconds to wait for managed gateway shutdown",
    )
    local_actions = set_parser.add_mutually_exclusive_group()
    local_actions.add_argument(
        "--enable-local-actions",
        dest="enable_local_actions",
        action="store_true",
        default=None,
        help="allow local action_router handlers before PicoClaw",
    )
    local_actions.add_argument(
        "--disable-local-actions",
        dest="enable_local_actions",
        action="store_false",
        help="send replies to the configured brain without local action routing",
    )
    set_parser.set_defaults(func=_cmd_set)

    args = parser.parse_args()
    args.func(args)


def _add_subcommand_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help=f"config file path, default: {DEFAULT_CONFIG_PATH}",
    )


def _cmd_init(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser()
    if config_path.exists() and not args.force:
        raise SystemExit(f"config already exists: {config_path}")

    example_path = _find_example_path(args.example)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_path, config_path)
    print(f"created config: {config_path}")


def _cmd_show(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser()
    data = _read_config(config_path)
    values = _config_values(data)

    if args.json:
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return

    print(f"config: {config_path}")
    for key, value in values["mimo"].items():
        print(f"{MODEL_FIELDS[key]}={value}")
    for key, value in values["audio"].items():
        print(f"{AUDIO_FIELDS[key]}={value}")
    for key, value in values["wakeword"].items():
        print(f"{WAKEWORD_FIELDS[key]}={value}")
    for key, value in values["runtime"].items():
        print(f"{CONFIG_LABELS[key]}={value}")
    env_state = "set" if os.getenv("MIMO_API_KEY", "").strip() else "not set"
    print(f"MIMO_API_KEY={env_state}")
    pico_token_state = "set" if os.getenv("PICOCLAW_TOKEN", "").strip() else "not set"
    print(f"PICOCLAW_TOKEN={pico_token_state}")


def _cmd_set(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser()
    data = _read_config(config_path)
    mimo = data.setdefault("mimo", {})
    audio = data.setdefault("audio", {})
    wakeword = data.setdefault("wakeword", {})
    runtime = data.setdefault("runtime", {})

    changes: list[str] = []
    for key, path in MODEL_FIELDS.items():
        value = getattr(args, key)
        if value is None:
            continue
        old_value = str(mimo.get(key, ""))
        mimo[key] = value
        if old_value != value and key == "api_key":
            changes.append(f"{path}: {_secret_state(old_value)} -> {_secret_state(value)}")
        elif old_value != value:
            changes.append(f"{path}: {old_value} -> {value}")

    if getattr(args, "clear_api_key", False):
        old_value = str(mimo.get("api_key", ""))
        mimo["api_key"] = ""
        if old_value:
            changes.append(f"{MODEL_FIELDS['api_key']}: set -> not set")

    for key, path in AUDIO_FIELDS.items():
        value = getattr(args, key, None)
        if value is None:
            continue
        old_value = str(audio.get(key, ""))
        audio[key] = value
        if old_value != value:
            changes.append(f"{path}: {old_value} -> {value}")

    for key, path in RUNTIME_FIELDS.items():
        value = getattr(args, key, None)
        if value is None:
            continue
        old_value = runtime.get(key, "")
        runtime[key] = value
        if old_value != value:
            changes.append(f"{path}: {old_value} -> {value}")

    for key, path in SPOKEN_REPLY_FIELDS.items():
        value = getattr(args, key, None)
        if value is None:
            continue
        old_value = runtime.get(key, "")
        runtime[key] = value
        if old_value != value:
            changes.append(f"{path}: {old_value} -> {value}")

    for key, path in WAKEWORD_FIELDS.items():
        value = getattr(args, key, None)
        if value is None:
            continue
        old_value = wakeword.get(key, "")
        wakeword[key] = value
        if old_value != value:
            changes.append(f"{path}: {old_value} -> {value}")

    if not changes:
        print("no configuration changes")
        return

    _write_config(config_path, data)
    print(f"updated config: {config_path}")
    for change in changes:
        print(change)


def _read_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise SystemExit(f"config not found: {path}. Run init first.") from exc

    if not isinstance(data, dict):
        raise SystemExit(f"config root must be a JSON object: {path}")
    return data


def _write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _config_values(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mimo = data.get("mimo", {})
    if not isinstance(mimo, dict):
        mimo = {}
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
    wakeword = data.get("wakeword", {})
    if not isinstance(wakeword, dict):
        wakeword = {}
    audio = data.get("audio", {})
    if not isinstance(audio, dict):
        audio = {}
    return {
        "mimo": {
            key: _mimo_value(mimo, key)
            for key in MODEL_FIELDS
        },
        "audio": {
            key: _audio_value(audio, key)
            for key in AUDIO_FIELDS
        },
        "wakeword": {
            key: _wakeword_value(wakeword, key)
            for key in WAKEWORD_FIELDS
        },
        "runtime": {
            key: _runtime_value(runtime, key)
            for key in RUNTIME_FIELDS
        },
    }


def _mimo_value(mimo: dict[str, Any], key: str) -> str:
    value = str(mimo.get(key, ""))
    if key == "api_key":
        return _secret_state(value)
    return value


def _secret_state(value: str) -> str:
    return "set" if value.strip() else "not set"


def _audio_value(audio: dict[str, Any], key: str) -> str:
    return str(audio.get(key, ""))


def _runtime_value(runtime: dict[str, Any], key: str) -> Any:
    value = runtime.get(key, "")
    if key in {"enable_local_actions", "picoclaw_manage_gateway"}:
        return bool(value)
    return value


def _wakeword_value(wakeword: dict[str, Any], key: str) -> str | float:
    value = wakeword.get(key, "")
    if key == "session_timeout_seconds" and value != "":
        return float(value)
    return str(value)


def _find_example_path(path: str) -> Path:
    if path:
        candidate = Path(path).expanduser()
        if candidate.exists():
            return candidate
        raise SystemExit(f"example config not found: {candidate}")

    candidates = [
        Path.cwd() / "config.example.json",
        Path(__file__).resolve().parents[2] / "config.example.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("config.example.json not found")


if __name__ == "__main__":
    main()
