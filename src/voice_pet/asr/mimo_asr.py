from __future__ import annotations

import base64
from pathlib import Path

from ..mimo_endpoint import chat_completions_url, post_json_without_proxy


class MimoASR:
    def __init__(self, api_key: str, api_base: str, model: str, language: str = "zh", timeout: int = 120):
        self.api_key = api_key
        self.api_base = chat_completions_url(api_base)
        self.model = model
        self.language = language
        self.timeout = timeout

    def transcribe_file(self, path: str) -> str:
        file_path = Path(path)
        audio_bytes = file_path.read_bytes()
        mime = _mime_from_suffix(file_path.suffix.lower())
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:{mime};base64,{audio_b64}"
                            },
                        }
                    ],
                }
            ],
            "extra_body": {
                "asr_options": {
                    "language": self.language,
                }
            },
        }
        resp = post_json_without_proxy(
            self.api_base,
            payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            return " ".join(str(item) for item in content).strip()
        return str(content).strip()


def _mime_from_suffix(suffix: str) -> str:
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".ogg":
        return "audio/ogg"
    return "audio/wav"
