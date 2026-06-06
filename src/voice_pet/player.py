from __future__ import annotations

import subprocess
from pathlib import Path


class AudioPlayer:
    def __init__(self, command: str = "aplay"):
        self.command = command

    def play_file(self, path: str) -> None:
        file_path = Path(path)
        subprocess.run([self.command, str(file_path)], check=True)
