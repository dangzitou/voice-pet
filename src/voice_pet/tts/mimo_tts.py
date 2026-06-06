from __future__ import annotations

import base64

import requests


class MimoTTS:
    def __init__(self, api_key: str, api_base: str, model: str, voice: str = "default_zh", fmt: str = "wav", timeout: int = 120):
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.voice = voice
        self.fmt = fmt
        self.timeout = timeout

    def synthesize(self, text: str) -> bytes:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "assistant", "content": text},
            ],
            "audio": {
                "format": self.fmt,
                "voice": self.voice,
            },
            "stream": False,
        }
        resp = requests.post(
            self.api_base,
            json=payload,
            headers={
                "Api-Key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        audio_data = data["choices"][0]["message"]["audio"]["data"]
        return base64.b64decode(audio_data)
