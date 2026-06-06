from __future__ import annotations

from typing import Protocol


class BrainAdapter(Protocol):
    def reply(self, text: str) -> str: ...
