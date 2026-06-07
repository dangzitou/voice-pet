from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "~/.picoclaw/voice-pet/config.json"


MODEL_FIELDS = {
    "api_base": "mimo.api_base",
    "asr_model": "mimo.asr_model",
    "tts_model": "mimo.tts_model",
    "llm_model": "mimo.llm_model",
    "language": "mimo.language",
    "tts_voice": "mimo.tts_voice",
    "tts_format": "mimo.tts_format",
}


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
    set_parser.add_argument("--api-base", dest="api_base", help="MiMo API endpoint")
    set_parser.add_argument("--asr-model", dest="asr_model", help="ASR model name")
    set_parser.add_argument("--tts-model", dest="tts_model", help="TTS model name")
    set_parser.add_argument("--model", "--llm-model", dest="llm_model", help="text reply model name")
    set_parser.add_argument("--language", dest="language", help="ASR language, for example: zh")
    set_parser.add_argument("--tts-voice", dest="tts_voice", help="TTS voice name")
    set_parser.add_argument("--tts-format", dest="tts_format", help="TTS audio format, for example: wav")
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
    values = _model_values(data)

    if args.json:
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return

    print(f"config: {config_path}")
    for key, value in values.items():
        print(f"{MODEL_FIELDS[key]}={value}")
    env_state = "set" if os.getenv("MIMO_API_KEY", "").strip() else "not set"
    print(f"MIMO_API_KEY={env_state}")


def _cmd_set(args: argparse.Namespace) -> None:
    config_path = Path(args.config).expanduser()
    data = _read_config(config_path)
    mimo = data.setdefault("mimo", {})

    changes: list[str] = []
    for key, path in MODEL_FIELDS.items():
        value = getattr(args, key)
        if value is None:
            continue
        old_value = str(mimo.get(key, ""))
        mimo[key] = value
        if old_value != value:
            changes.append(f"{path}: {old_value} -> {value}")

    if not changes:
        print("no model configuration changes")
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


def _model_values(data: dict[str, Any]) -> dict[str, str]:
    mimo = data.get("mimo", {})
    if not isinstance(mimo, dict):
        mimo = {}
    return {key: str(mimo.get(key, "")) for key in MODEL_FIELDS}


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
