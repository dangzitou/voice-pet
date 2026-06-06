from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WakewordResult:
    matched: bool
    alias: str = ""
    cleaned_text: str = ""


class WakewordDetector:
    def __init__(self, aliases: list[str]):
        normalized = []
        for alias in aliases:
            alias = alias.strip().lower()
            if alias:
                normalized.append(alias)
        self.aliases = sorted(set(normalized), key=len, reverse=True)

    def detect(self, text: str) -> WakewordResult:
        normalized = " ".join(text.strip().lower().split())
        for alias in self.aliases:
            if alias in normalized:
                cleaned = normalized.replace(alias, " ").strip(" ，。,.!?！？")
                cleaned = " ".join(cleaned.split())
                return WakewordResult(matched=True, alias=alias, cleaned_text=cleaned)
        return WakewordResult(matched=False, cleaned_text=normalized)
