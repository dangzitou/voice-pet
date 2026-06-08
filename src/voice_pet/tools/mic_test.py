from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import wave

from ..asr.mimo_asr import MimoASR
from ..config import load_config


DEFAULT_CONFIG_PATH = "~/.picoclaw/voice-pet/config.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Show a live microphone volume meter")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"config path, default: {DEFAULT_CONFIG_PATH}")
    parser.add_argument("--device", default="", help="ALSA capture device passed to arecord -D")
    parser.add_argument("--sample-rate", type=int, default=0, help="sample rate, default from config")
    parser.add_argument("--channels", type=int, default=0, help="channel count, default from config")
    parser.add_argument("--chunk-ms", type=int, default=100, help="meter refresh chunk in milliseconds")
    parser.add_argument("--duration", type=float, default=0.0, help="seconds to run; 0 means run until Ctrl+C")
    parser.add_argument("--scale", type=float, default=0.0, help="RMS value that fills the meter; default is threshold * 4")
    parser.add_argument("--threshold", type=float, default=0.0, help="active threshold; default from config")
    parser.add_argument("--silence-threshold", type=float, default=0.0, help="RMS treated as silence; default from config")
    parser.add_argument("--silence-seconds", type=float, default=0.0, help="seconds of silence to end ASR capture")
    parser.add_argument("--min-seconds", type=float, default=0.4, help="minimum utterance length before silence can end it")
    parser.add_argument("--max-seconds", type=float, default=12.0, help="maximum utterance length; 0 disables")
    parser.add_argument("--pre-roll-seconds", type=float, default=0.3, help="audio kept before threshold crossing")
    parser.add_argument("--output-dir", default="", help="directory for captured wav files, default runtime/mic-test")
    parser.add_argument("--no-asr", action="store_true", help="only show the meter; do not transcribe captured speech")
    parser.add_argument("--width", type=int, default=40, help="meter character width")
    parser.add_argument("--line", action="store_true", help="print one line per sample instead of updating in place")
    parser.add_argument("--list-devices", action="store_true", help="print arecord capture devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        _run(["arecord", "-l"])
        print("")
        _run(["arecord", "-L"])
        return

    config = load_config(args.config)
    audio = config.get("audio", {})
    device = args.device or str(audio.get("record_device", ""))
    sample_rate = args.sample_rate or int(audio.get("sample_rate", 16000))
    channels = args.channels or int(audio.get("channels", 1))
    threshold = args.threshold or float(audio.get("voice_start_threshold", 0))
    silence_threshold = args.silence_threshold or float(audio.get("silence_threshold", max(1, threshold * 0.8)))
    silence_seconds = args.silence_seconds or float(audio.get("wake_silence_seconds", audio.get("silence_seconds", 1.0)))
    output_dir = Path(
        args.output_dir
        or Path(str(config.get("runtime", {}).get("work_dir", "~/.picoclaw/voice-pet/runtime"))).expanduser() / "mic-test"
    ).expanduser()

    asr: MimoASR | None = None
    if not args.no_asr:
        mimo = config.get("mimo", {})
        api_key = str(mimo.get("api_key", "")).strip()
        if not api_key:
            raise SystemExit("mimo.api_key or MIMO_API_KEY is required for ASR; use --no-asr to only show the meter")
        asr = MimoASR(
            api_key,
            str(mimo.get("api_base", "")),
            str(mimo.get("asr_model", "mimo-v2.5-asr")),
            str(mimo.get("language", "zh")),
            int(config.get("runtime", {}).get("request_timeout_seconds", 120)),
        )

    if not shutil.which("arecord"):
        raise SystemExit("arecord not found; install alsa-utils first")

    cmd = [
        "arecord",
        "-q",
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        str(channels),
        "-t",
        "raw",
    ]
    if device:
        cmd[1:1] = ["-D", device]

    chunk_ms = max(20, args.chunk_ms)
    chunk_frames = max(1, int(sample_rate * chunk_ms / 1000))
    chunk_bytes = chunk_frames * channels * 2
    scale = max(1.0, args.scale or threshold * 4 or 1200.0)
    width = max(8, args.width)
    max_seconds = None if args.max_seconds <= 0 else args.max_seconds
    pre_roll_chunks = max(0, int(args.pre_roll_seconds * 1000 / chunk_ms))
    pre_roll: deque[bytes] = deque(maxlen=pre_roll_chunks)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"device={device or 'default'} sample_rate={sample_rate} channels={channels}")
    print(
        f"threshold={threshold:.0f} silence_threshold={silence_threshold:.0f} "
        f"silence_seconds={silence_seconds:.1f} scale={scale:.0f} chunk_ms={chunk_ms}"
    )
    print(f"asr={'off' if asr is None else 'on'} output_dir={output_dir}")
    print("Speak near the microphone. Press Ctrl+C to stop.")

    started_at = time.monotonic()
    max_rms = 0.0
    max_peak = 0
    recording = False
    speech_started_at = 0.0
    silence_started_at: float | None = None
    utterance_frames: list[bytes] = []
    utterance_peak_rms = 0.0
    utterance_index = 0
    futures: list[Future[str]] = []
    last_event = ""
    executor = ThreadPoolExecutor(max_workers=1) if asr is not None else None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.stdout is None:
        proc.terminate()
        raise SystemExit("arecord stdout unavailable")

    try:
        while True:
            if args.duration > 0 and time.monotonic() - started_at >= args.duration:
                break
            chunk = proc.stdout.read(chunk_bytes)
            if not chunk:
                break
            rms, peak = _pcm_stats(chunk)
            max_rms = max(max_rms, rms)
            max_peak = max(max_peak, peak)
            now = time.monotonic()
            status = "ACTIVE" if threshold > 0 and rms >= threshold else "quiet"
            if not recording:
                if threshold > 0 and rms >= threshold:
                    recording = True
                    speech_started_at = now
                    silence_started_at = None
                    utterance_peak_rms = rms
                    utterance_frames = list(pre_roll)
                    utterance_frames.append(chunk)
                    last_event = f"speech_start rms={rms:.1f}"
                    _print_event(args.line, f"[mic-test] {last_event} peak={peak} threshold={threshold:.0f}")
                else:
                    if pre_roll_chunks:
                        pre_roll.append(chunk)
            else:
                utterance_frames.append(chunk)
                utterance_peak_rms = max(utterance_peak_rms, rms)
                elapsed_speech = now - speech_started_at
                end_reason = ""
                if rms < silence_threshold and elapsed_speech >= args.min_seconds:
                    if silence_started_at is None:
                        silence_started_at = now
                    elif now - silence_started_at >= silence_seconds:
                        end_reason = "silence"
                else:
                    silence_started_at = None
                if max_seconds is not None and elapsed_speech >= max_seconds:
                    end_reason = "max_seconds"
                if end_reason:
                    utterance_index += 1
                    wav_path = output_dir / f"utterance-{utterance_index:03d}.wav"
                    _write_wav(wav_path, utterance_frames, sample_rate, channels)
                    last_event = (
                        f"speech_end #{utterance_index:03d} reason={end_reason} "
                        f"duration={elapsed_speech:.2f}s peak_rms={utterance_peak_rms:.1f}"
                    )
                    _print_event(args.line, f"[mic-test] {last_event} wav={wav_path}")
                    if asr is not None and executor is not None:
                        last_event = f"asr #{utterance_index:03d} transcribing"
                        futures.append(
                            executor.submit(_transcribe_and_print, asr, wav_path, utterance_index, args.line)
                        )
                    recording = False
                    utterance_frames = []
                    pre_roll.clear()

            finished_messages = _drain_finished_futures(futures)
            if finished_messages:
                last_event = finished_messages[-1]
            elapsed = time.monotonic() - started_at
            line = (
                f"{elapsed:7.2f}s "
                f"{_bar(rms, scale, width)} "
                f"rms={rms:7.1f} peak={peak:5d} "
                f"max_rms={max_rms:7.1f} max_peak={max_peak:5d} "
                f"dbfs={_dbfs(rms):6.1f} {status}{' REC' if recording else ''}"
            )
            if last_event:
                line = f"{line} | {last_event}"
            if args.line:
                print(line, flush=True)
            else:
                if finished_messages:
                    print("\r" + _fit_terminal_line(""), end="\r", flush=True)
                    for message in finished_messages:
                        print(message, flush=True)
                print("\r" + _fit_terminal_line(line), end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
        if executor is not None:
            executor.shutdown(wait=True)

    if not args.line:
        print("")

    stderr = b""
    if proc.stderr is not None:
        try:
            stderr = proc.stderr.read()
        except OSError:
            stderr = b""
    if proc.returncode not in (0, -15, -2, None) and stderr:
        sys.stderr.write(stderr.decode("utf-8", errors="replace"))


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=False)


def _print_event(line_mode: bool, message: str) -> None:
    if line_mode:
        print(message, flush=True)


def _drain_finished_futures(futures: list[Future[str]]) -> list[str]:
    messages = []
    pending = []
    for future in futures:
        if future.done():
            messages.append(future.result())
        else:
            pending.append(future)
    futures[:] = pending
    return messages


def _transcribe_and_print(asr: MimoASR, wav_path: Path, index: int, line_mode: bool) -> str:
    started_at = time.monotonic()
    _print_event(line_mode, f"[asr:{index:03d}] transcribing {wav_path}")
    try:
        text = asr.transcribe_file(str(wav_path))
        elapsed_ms = (time.monotonic() - started_at) * 1000
        message = f"asr #{index:03d}: {text or '<empty>'} ({elapsed_ms:.0f}ms)"
        _print_event(line_mode, f"[asr:{index:03d}] text={text or '<empty>'} elapsed={elapsed_ms:.0f}ms")
        return message
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        message = f"asr #{index:03d} failed ({elapsed_ms:.0f}ms): {exc}"
        _print_event(line_mode, f"[asr:{index:03d}] failed elapsed={elapsed_ms:.0f}ms error={exc}")
        return message


def _pcm_stats(pcm: bytes) -> tuple[float, int]:
    sample_count = len(pcm) // 2
    if sample_count <= 0:
        return 0.0, 0
    total = 0
    peak = 0
    for (sample,) in struct.iter_unpack("<h", pcm[: sample_count * 2]):
        abs_sample = abs(sample)
        peak = max(peak, abs_sample)
        total += sample * sample
    return math.sqrt(total / sample_count), peak


def _bar(value: float, scale: float, width: int) -> str:
    filled = min(width, max(0, int(round(value / scale * width))))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _dbfs(rms: float) -> float:
    if rms <= 0:
        return -120.0
    return 20 * math.log10(rms / 32768.0)


def _fit_terminal_line(line: str) -> str:
    width = shutil.get_terminal_size(fallback=(120, 24)).columns
    if width <= 1:
        return line
    if len(line) >= width:
        return line[: width - 1]
    return line + " " * (width - len(line) - 1)


def _write_wav(path: Path, frames: list[bytes], sample_rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(b"".join(frames))


if __name__ == "__main__":
    main()
