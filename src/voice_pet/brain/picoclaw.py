from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

VOICE_REPLY_PROMPT = (
    "请用非常简短、自然、口语化的中文回答。"
    "不要使用 markdown、emoji、列表、代码块。"
    "通常控制在 1 到 2 句话，优先直接回答。"
)


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
        content = f"{VOICE_REPLY_PROMPT}\n\n用户问题：{text}"
        script = str(Path(self.cfg.node_script).expanduser())
        proc = subprocess.run(
            [
                "node",
                script,
                self.cfg.url,
                self.cfg.token,
                self.cfg.session_id,
                content,
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
