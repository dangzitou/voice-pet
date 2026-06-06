from __future__ import annotations

import subprocess
import time
from pathlib import Path


class AudioCapture:
    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        device: str = "",
        silence_threshold: int = 500,
        silence_seconds: float = 1.2,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self.silence_threshold = silence_threshold
        self.silence_seconds = silence_seconds

    def record_for_duration(self, path: str, seconds: float) -> str:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            str(self.channels),
            "-d",
            str(max(1, int(round(seconds)))),
            str(output),
        ]
        if self.device:
            cmd[1:1] = ["-D", self.device]
        subprocess.run(cmd, check=True)
        return str(output)

    def record_until_silence(self, path: str, max_seconds: float, min_seconds: float = 1.0) -> str:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            str(self.channels),
            str(output),
        ]
        if self.device:
            cmd[1:1] = ["-D", self.device]

        proc = subprocess.Popen(cmd)
        started_at = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - started_at
                if elapsed >= max_seconds:
                    break
                rms = _read_rms(output)
                if elapsed >= min_seconds and rms < self.silence_threshold:
                    silence_begin = time.monotonic()
                    while time.monotonic() - silence_begin < self.silence_seconds:
                        time.sleep(0.15)
                        rms = _read_rms(output)
                        if rms >= self.silence_threshold:
                            break
                    else:
                        break
                time.sleep(0.15)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        return str(output)


def _read_rms(path: Path) -> float:
    if not path.exists() or path.stat().st_size < 44:
        return 0.0
    try:
        with path.open("rb") as f:
            data = f.read()
        pcm = data[44:]
        if not pcm:
            return 0.0
        sample_count = len(pcm) // 2
        if sample_count == 0:
            return 0.0
        total = 0
        for i in range(0, sample_count * 2, 2):
            sample = int.from_bytes(pcm[i:i + 2], byteorder="little", signed=True)
            total += sample * sample
        return (total / sample_count) ** 0.5
    except OSError:
        return 0.0
