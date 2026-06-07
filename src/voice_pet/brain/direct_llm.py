from __future__ import annotations

from ..mimo_endpoint import chat_completions_url, post_json_without_proxy

VOICE_REPLY_SYSTEM = (
    "你现在是一个桌面语音助手。"
    "用户是在和你口头对话，请用非常简短、自然、口语化的中文回复。"
    "不要使用 markdown、emoji、列表、代码块。"
    "通常控制在 1 到 2 句话。"
)


class DirectLLMAdapter:
    def __init__(self, api_key: str, api_base: str, model: str, timeout: int = 120):
        self.api_key = api_key
        self.api_base = chat_completions_url(api_base)
        self.model = model
        self.timeout = timeout

    def reply(self, text: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": VOICE_REPLY_SYSTEM},
                {"role": "user", "content": text},
            ],
            "stream": False,
        }
        resp = post_json_without_proxy(
            self.api_base,
            payload,
            headers={
                "Api-Key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
