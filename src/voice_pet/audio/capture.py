from __future__ import annotations

from collections import deque
from queue import Empty, Full, Queue
import subprocess
from threading import Event, Lock, Thread
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
        self._stream_proc: subprocess.Popen[bytes] | None = None
        self._stream_thread: Thread | None = None
        self._stream_stop: Event | None = None
        self._stream_chunk_bytes = 0
        self._stream_lock = Lock()
        self._chunks: Queue[tuple[float, bytes]] = Queue(maxsize=300)

    def record_next_utterance(
        self,
        path: str,
        max_seconds: float | None = None,
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
        explicit_start_threshold = start_threshold is not None and start_threshold > 0
        explicit_silence_threshold = silence_threshold is not None and silence_threshold > 0
        start_threshold = self.silence_threshold if start_threshold is None else start_threshold
        silence_threshold = self.silence_threshold if silence_threshold is None else silence_threshold
        silence_seconds = self.silence_seconds if silence_seconds is None else silence_seconds
        max_seconds = None if max_seconds is None or max_seconds <= 0 else max_seconds
        pre_roll_chunks = max(0, int(pre_roll_seconds * 1000 / max(20, chunk_ms)))
        pre_roll: deque[bytes] = deque(maxlen=pre_roll_chunks)
        noise_samples: deque[float] = deque(maxlen=max(8, int(2000 / max(20, chunk_ms))))
        frames: list[bytes] = []
        waiting_started_at = time.monotonic()
        speech_started_at = 0.0
        silence_started_at: float | None = None
        active_silence_threshold = float(silence_threshold)
        peak_rms = 0.0
        waiting_peak_rms = 0.0
        last_waiting_log_at = 0.0
        self._ensure_stream(chunk_bytes)
        self._drain_chunks()

        while True:
            try:
                read_at, chunk = self._read_chunk(chunk_bytes, timeout=0.2)
            except TimeoutError:
                if (
                    not speech_started_at
                    and start_timeout_seconds is not None
                    and time.monotonic() - waiting_started_at >= start_timeout_seconds
                ):
                    raise TimeoutError("no speech detected before timeout")
                continue

            rms = _read_pcm_rms(chunk)
            now = read_at
            if not speech_started_at:
                noise_samples.append(rms)
                noise_floor = _median(noise_samples)
                effective_start_threshold = (
                    float(start_threshold)
                    if explicit_start_threshold
                    else _effective_threshold(
                        configured=float(start_threshold),
                        noise_floor=noise_floor,
                        multiplier=2.0,
                        margin=250.0,
                        minimum=650.0,
                    )
                )
                waiting_peak_rms = max(waiting_peak_rms, rms)
                if rms >= effective_start_threshold:
                    speech_started_at = now
                    active_silence_threshold = (
                        float(silence_threshold)
                        if explicit_silence_threshold
                        else _effective_threshold(
                            configured=float(silence_threshold),
                            noise_floor=noise_floor,
                            multiplier=1.5,
                            margin=150.0,
                            minimum=500.0,
                        )
                    )
                    peak_rms = rms
                    print(
                        "[audio] speech_start "
                        f"rms={rms:.0f} noise={noise_floor:.0f} "
                        f"start_threshold={effective_start_threshold:.0f} "
                        f"silence_threshold={active_silence_threshold:.0f}"
                    )
                    frames.extend(pre_roll)
                    pre_roll.clear()
                    frames.append(chunk)
                else:
                    if pre_roll_chunks:
                        pre_roll.append(chunk)
                    if now - last_waiting_log_at >= 2.0:
                        print(
                            "[audio] waiting_for_speech "
                            f"rms={rms:.0f} peak={waiting_peak_rms:.0f} "
                            f"noise={noise_floor:.0f} "
                            f"start_threshold={effective_start_threshold:.0f}"
                        )
                        waiting_peak_rms = 0.0
                        last_waiting_log_at = now
                    if (
                        start_timeout_seconds is not None
                        and now - waiting_started_at >= start_timeout_seconds
                    ):
                        raise TimeoutError("no speech detected before timeout")
                continue

            frames.append(chunk)
            peak_rms = max(peak_rms, rms)
            elapsed = now - speech_started_at
            if rms >= active_silence_threshold:
                silence_started_at = None
            elif elapsed >= min_seconds:
                if silence_started_at is None:
                    silence_started_at = now
                elif now - silence_started_at >= silence_seconds:
                    print(
                        "[audio] speech_end reason=silence "
                        f"duration={elapsed:.2f}s peak={peak_rms:.0f} "
                        f"silence_threshold={active_silence_threshold:.0f}"
                    )
                    break

            if max_seconds is not None and elapsed >= max_seconds:
                print(
                    "[audio] speech_end reason=max_seconds "
                    f"duration={elapsed:.2f}s peak={peak_rms:.0f} "
                    f"max_seconds={max_seconds:.1f}s"
                )
                break

        if not frames:
            raise RuntimeError("no utterance captured")

        _write_wav(output, frames, self.sample_rate, self.channels)
        return str(output)

    def reset_stream(self) -> None:
        self._drain_chunks()

    def close(self) -> None:
        with self._stream_lock:
            self._close_locked()
        self._drain_chunks()

    def _ensure_stream(self, chunk_bytes: int) -> None:
        with self._stream_lock:
            if (
                self._stream_proc is not None
                and self._stream_proc.poll() is None
                and self._stream_chunk_bytes == chunk_bytes
            ):
                return
            self._close_locked()

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

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if proc.stdout is None:
                proc.terminate()
                raise RuntimeError("arecord stdout is unavailable")

            stop_event = Event()
            thread = Thread(
                target=self._read_stream,
                args=(proc, stop_event, chunk_bytes),
                daemon=True,
            )
            self._stream_proc = proc
            self._stream_stop = stop_event
            self._stream_thread = thread
            self._stream_chunk_bytes = chunk_bytes
            thread.start()

    def _read_chunk(self, chunk_bytes: int, timeout: float) -> tuple[float, bytes]:
        self._ensure_stream(chunk_bytes)
        try:
            return self._chunks.get(timeout=timeout)
        except Empty:
            proc = self._stream_proc
            if proc is None or proc.poll() is not None:
                self._ensure_stream(chunk_bytes)
            raise TimeoutError("no audio chunk available")

    def _read_stream(self, proc: subprocess.Popen[bytes], stop_event: Event, chunk_bytes: int) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        while not stop_event.is_set():
            chunk = stdout.read(chunk_bytes)
            if not chunk:
                break
            self._put_chunk((time.monotonic(), chunk))

    def _put_chunk(self, chunk: tuple[float, bytes]) -> None:
        try:
            self._chunks.put_nowait(chunk)
            return
        except Full:
            pass
        try:
            self._chunks.get_nowait()
        except Empty:
            pass
        try:
            self._chunks.put_nowait(chunk)
        except Full:
            pass

    def _drain_chunks(self) -> None:
        while True:
            try:
                self._chunks.get_nowait()
            except Empty:
                return

    def _close_locked(self) -> None:
        proc = self._stream_proc
        thread = self._stream_thread
        stop_event = self._stream_stop
        self._stream_proc = None
        self._stream_thread = None
        self._stream_stop = None
        self._stream_chunk_bytes = 0
        if stop_event is not None:
            stop_event.set()
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        if thread is not None:
            thread.join(timeout=1)

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


def _median(values: deque[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _effective_threshold(
    *,
    configured: float,
    noise_floor: float,
    multiplier: float,
    margin: float,
    minimum: float,
) -> float:
    adaptive = max(minimum, noise_floor * multiplier + margin)
    if configured <= 0:
        return adaptive
    if noise_floor <= configured * 0.9:
        return configured
    return max(configured, noise_floor + max(50.0, configured * 0.25))


def _write_wav(path: Path, frames: list[bytes], sample_rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(b"".join(frames))
