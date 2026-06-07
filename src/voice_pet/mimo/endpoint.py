from __future__ import annotations

from typing import Any

import requests


def chat_completions_url(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    if not base:
        return base
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def post_json_without_proxy(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False
        return session.post(url, json=payload, headers=headers, timeout=timeout)
