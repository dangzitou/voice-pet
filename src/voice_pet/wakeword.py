from __future__ import annotations

from dataclasses import dataclass
import re


_PUNCTUATION = re.compile(r"[\s,，.。!！?？;；:：'\"“”‘’()\[\]{}<>《》、\-_/\\|]+")
_COMMON_WAKE_EQUIVALENTS = {
    "小爱": ["小艾", "晓爱", "晓艾", "小哎", "晓哎", "小碍", "小唉", "你好你好", "你号你号"],
    "小艾": ["小爱", "晓爱", "晓艾", "小哎", "晓哎", "小碍", "小唉", "你好你好", "你号你号"],
    "xiaoai": ["小爱", "小艾", "晓爱", "晓艾", "小哎", "晓哎", "你好你好", "你号你号"],
}


@dataclass(slots=True)
class WakewordResult:
    matched: bool
    alias: str = ""
    cleaned_text: str = ""


class WakewordDetector:
    def __init__(self, aliases: list[str]):
        normalized = []
        for alias in aliases:
            alias = _compact(alias)
            if alias:
                normalized.append(alias)
                normalized.extend(_COMMON_WAKE_EQUIVALENTS.get(alias, []))
        self.aliases = sorted(set(normalized), key=len, reverse=True)

    def detect(self, text: str) -> WakewordResult:
        normalized = _compact(text)
        for alias in self.aliases:
            if alias in normalized:
                cleaned = normalized.replace(alias, "")
                return WakewordResult(matched=True, alias=alias, cleaned_text=cleaned)
        return WakewordResult(matched=False, cleaned_text=normalized)


def _compact(text: str) -> str:
    return _PUNCTUATION.sub("", text.strip().lower())
