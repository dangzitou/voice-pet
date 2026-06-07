from __future__ import annotations

from typing import Callable


class ActionRouter:
    def __init__(self):
        self._handlers: list[tuple[Callable[[str], bool], Callable[[str], str]]] = []

    def register(self, matcher: Callable[[str], bool], handler: Callable[[str], str]) -> None:
        self._handlers.append((matcher, handler))

    def handle(self, text: str) -> str | None:
        for matcher, handler in self._handlers:
            if matcher(text):
                return handler(text)
        return None


def build_default_router() -> ActionRouter:
    router = ActionRouter()
    router.register(
        lambda text: "天气" in text,
        lambda text: "主人，我这版还没真正接天气接口，不过我已经预留好动作路由啦。",
    )
    router.register(
        lambda text: "放首歌" in text or "音乐" in text,
        lambda text: "主人，我还没接真实音乐播放器，不过下一步就可以把它挂进来。",
    )
    return router
