from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

VOICE_REPLY_PROMPT = (
    "你是桌面语音助手，请用自然口语中文回答。"
    "回答要短，但必须有信息量，不能用空泛话糊弄。"
    "简单问题用 1 到 2 句话；新闻、天气、整理、对比、步骤类问题可以给 3 到 5 个短要点。"
    "不要使用 markdown、emoji、代码块；可以用顿号、分号或一二三这种口语编号。"
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
