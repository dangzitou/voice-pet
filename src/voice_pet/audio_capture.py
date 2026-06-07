from __future__ import annotations

from collections import deque
import subprocess
import time
import wave
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

    def record_next_utterance(
        self,
        path: str,
        max_seconds: float,
        min_seconds: float = 0.5,
        chunk_ms: int = 100,
        pre_roll_seconds: float = 0.3,
        start_timeout_seconds: float | None = None,
        start_threshold: int | None = None,
        silence_threshold: int | None = None,
        silence_seconds: float | None = None,
    ) -> str:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)

        frame_bytes = self.channels * 2
        chunk_frames = max(1, int(self.sample_rate * max(20, chunk_ms) / 1000))
        chunk_bytes = chunk_frames * frame_bytes
        start_threshold = self.silence_threshold if start_threshold is None else start_threshold
        silence_threshold = self.silence_threshold if silence_threshold is None else silence_threshold
        silence_seconds = self.silence_seconds if silence_seconds is None else silence_seconds
        pre_roll_chunks = max(0, int(pre_roll_seconds * 1000 / max(20, chunk_ms)))
        pre_roll: deque[bytes] = deque(maxlen=pre_roll_chunks)

        cmd = [
            "arecord",
            "-q",
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            str(self.channels),
            "-t",
            "raw",
        ]
        if self.device:
            cmd[1:1] = ["-D", self.device]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        frames: list[bytes] = []
        waiting_started_at = time.monotonic()
        speech_started_at = 0.0
        silence_started_at: float | None = None
        try:
            if proc.stdout is None:
                raise RuntimeError("arecord stdout is unavailable")

            while True:
                chunk = proc.stdout.read(chunk_bytes)
                if not chunk:
                    break

                rms = _read_pcm_rms(chunk)
                now = time.monotonic()
                if not speech_started_at:
                    if rms >= start_threshold:
                        speech_started_at = now
                        frames.extend(pre_roll)
                        pre_roll.clear()
                        frames.append(chunk)
                    else:
                        if pre_roll_chunks:
                            pre_roll.append(chunk)
                        if (
                            start_timeout_seconds is not None
                            and now - waiting_started_at >= start_timeout_seconds
                        ):
                            raise TimeoutError("no speech detected before timeout")
                    continue

                frames.append(chunk)
                elapsed = now - speech_started_at
                if rms >= silence_threshold:
                    silence_started_at = None
                elif elapsed >= min_seconds:
                    if silence_started_at is None:
                        silence_started_at = now
                    elif now - silence_started_at >= silence_seconds:
                        break

                if elapsed >= max_seconds:
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

        if not frames:
            raise RuntimeError("no utterance captured")

        _write_wav(output, frames, self.sample_rate, self.channels)
        return str(output)

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


def _read_pcm_rms(pcm: bytes) -> float:
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return 0.0
    total = 0
    for i in range(0, sample_count * 2, 2):
        sample = int.from_bytes(pcm[i:i + 2], byteorder="little", signed=True)
        total += sample * sample
    return (total / sample_count) ** 0.5


def _write_wav(path: Path, frames: list[bytes], sample_rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(b"".join(frames))
