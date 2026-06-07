from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class PicoBridgeConfig:
    url: str
    token: str
    session_id: str = "voice-pet"
    timeout_seconds: float = 30.0
    node_script: str = "~/.picoclaw/voice-pet/pico_bridge_once.js"


class PicoClawAdapter:
    def __init__(self, cfg: PicoBridgeConfig):
        self.cfg = cfg

    def reply(self, text: str) -> str:
        script = str(Path(self.cfg.node_script).expanduser())
        proc = subprocess.run(
            [
                "node",
                script,
                self.cfg.url,
                self.cfg.token,
                self.cfg.session_id,
                text,
                str(self.cfg.timeout_seconds),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=max(int(self.cfg.timeout_seconds) + 5, 10),
        )
        reply = proc.stdout.strip()
        if not reply:
            raise RuntimeError("empty PicoClaw reply")
        return reply
