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

    runtime = data.setdefault("runtime", {})
    runtime.setdefault("work_dir", str(Path("~/.picoclaw/voice-pet/runtime").expanduser()))
    Path(runtime["work_dir"]).mkdir(parents=True, exist_ok=True)

    return data
