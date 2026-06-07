from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class BrainAdapter(Protocol):
    def reply(self, text: str) -> str: ...


@dataclass(slots=True)
class BrainReply:
    text: str
    source: str = "brain"
