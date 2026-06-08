import json
import os
from pathlib import Path
from typing import Any


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(val) for key, val in value.items()}
    return value


def load_config(path: str) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    data = _expand(data)

    env_api_key = os.getenv("MIMO_API_KEY", "").strip()
    if env_api_key:
        data.setdefault("mimo", {})["api_key"] = env_api_key

    wakeword = data.setdefault("wakeword", {})
    _set_env_override(wakeword, "ack_text", "VOICE_PET_ACK_TEXT")
    _set_env_override(wakeword, "ack_audio_path", "VOICE_PET_ACK_AUDIO_PATH")
    _set_env_override(wakeword, "ack_texts", "VOICE_PET_ACK_TEXTS")
    _set_env_override(wakeword, "ack_audio_paths", "VOICE_PET_ACK_AUDIO_PATHS")
    _set_env_override(wakeword, "thinking_prompt_delay_seconds", "VOICE_PET_THINKING_PROMPT_DELAY_SECONDS")
    _set_env_override(wakeword, "thinking_prompt_max_delay_seconds", "VOICE_PET_THINKING_PROMPT_MAX_DELAY_SECONDS")

    audio = data.setdefault("audio", {})
    _set_env_override(audio, "playback_command", "VOICE_PET_PLAYBACK_COMMAND")
    _set_env_override(audio, "playback_device", "VOICE_PET_PLAYBACK_DEVICE")
    _set_env_override(audio, "playback_cooldown_seconds", "VOICE_PET_PLAYBACK_COOLDOWN_SECONDS")

    mimo = data.setdefault("mimo", {})
    _set_env_override(mimo, "tts_style_prompt", "VOICE_PET_TTS_STYLE_PROMPT")

    runtime = data.setdefault("runtime", {})
    _set_env_override(runtime, "brain", "VOICE_PET_BRAIN")
    _set_env_override(runtime, "picoclaw_ws_url", "PICOCLAW_WS_URL")
    _set_env_override(runtime, "picoclaw_token", "PICOCLAW_TOKEN")
    _set_env_override(runtime, "picoclaw_session_id", "PICOCLAW_SESSION_ID")
    _set_env_override(runtime, "picoclaw_node_script", "PICOCLAW_NODE_SCRIPT")
    _set_bool_env_override(runtime, "enable_local_actions", "VOICE_PET_ENABLE_LOCAL_ACTIONS")
    _set_bool_env_override(runtime, "picoclaw_manage_gateway", "PICOCLAW_MANAGE_GATEWAY")
    _set_env_override(runtime, "picoclaw_gateway_command", "PICOCLAW_GATEWAY_COMMAND")
    _set_env_override(runtime, "picoclaw_gateway_args", "PICOCLAW_GATEWAY_ARGS")
    _set_env_override(runtime, "picoclaw_gateway_cwd", "PICOCLAW_GATEWAY_CWD")
    _set_env_override(runtime, "picoclaw_gateway_ready_url", "PICOCLAW_GATEWAY_READY_URL")
    runtime.setdefault("work_dir", str(Path("~/.picoclaw/voice-pet/runtime").expanduser()))
    Path(runtime["work_dir"]).mkdir(parents=True, exist_ok=True)

    return data


def _set_env_override(target: dict[str, Any], key: str, env_name: str) -> None:
    value = os.getenv(env_name, "").strip()
    if value:
        target[key] = value


def _set_bool_env_override(target: dict[str, Any], key: str, env_name: str) -> None:
    value = os.getenv(env_name, "").strip().lower()
    if not value:
        return
    if value in {"1", "true", "yes", "on"}:
        target[key] = True
    elif value in {"0", "false", "no", "off"}:
        target[key] = False
    else:
        raise ValueError(f"invalid boolean env {env_name}: {value}")
