from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


class AudioPlayer:
    def __init__(self, command: str = "aplay", device: str = ""):
        self.command = command.strip() or "aplay"
        self.device = device.strip()

    def play_file(self, path: str) -> None:
        file_path = Path(path)
        argv = shlex.split(self.command)
        if self.device:
            argv.extend(["-D", self.device])
        argv.append(str(file_path))
        subprocess.run(argv, check=True)
