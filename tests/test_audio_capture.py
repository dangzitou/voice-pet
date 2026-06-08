from __future__ import annotations

import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from voice_pet.audio.capture import AudioCapture


class AudioCaptureTest(unittest.TestCase):
    def test_explicit_thresholds_are_not_raised_by_noise_floor(self) -> None:
        capture = AudioCapture(sample_rate=10, channels=1, silence_threshold=280, silence_seconds=0.1)
        chunks = [
            _chunk(290),
            _chunk(301),
            _chunk(260),
            _chunk(260),
            _chunk(260),
            _chunk(260),
        ]

        with TemporaryDirectory() as tmp, patch.object(capture, "_ensure_stream"), patch.object(
            capture, "_drain_chunks"
        ), patch.object(capture, "_read_chunk", side_effect=[(float(index), chunk) for index, chunk in enumerate(chunks)]):
            output = capture.record_next_utterance(
                str(Path(tmp) / "utterance.wav"),
                min_seconds=0.0,
                chunk_ms=100,
                pre_roll_seconds=0.0,
                start_threshold=300,
                silence_threshold=280,
                silence_seconds=0.1,
            )

            self.assertTrue(Path(output).is_file())


def _chunk(value: int, samples: int = 10) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))
