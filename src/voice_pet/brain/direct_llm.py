from __future__ import annotations

from ..mimo.endpoint import chat_completions_url, post_json_without_proxy

VOICE_REPLY_SYSTEM = (
    "你现在是一个桌面语音助手。"
    "用户是在和你口头对话，请用自然口语中文回复。"
    "回答要短，但必须有信息量，不能用空泛话糊弄。"
    "简单问题用 1 到 2 句话；新闻、天气、整理、对比、步骤类问题可以给 3 到 5 个短要点。"
    "不要使用 markdown、emoji、代码块；可以用顿号、分号或一二三这种口语编号。"
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
