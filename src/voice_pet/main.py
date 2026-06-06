from __future__ import annotations

import argparse

from .config import load_config
from .state_machine import VoicePetStateMachine


def main() -> None:
    parser = argparse.ArgumentParser(description="voice-pet MVP")
    parser.add_argument(
        "--config",
        default="~/.picoclaw/voice-pet/config.json",
        help="配置文件路径",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    machine = VoicePetStateMachine(config)
    machine.run()


if __name__ == "__main__":
    main()
