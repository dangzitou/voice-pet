from __future__ import annotations

import queue
import unittest
from threading import Event
from unittest.mock import patch

from voice_pet.brain.picoclaw import PicoBridgeConfig, PicoClawAdapter


class PicoClawAdapterTest(unittest.TestCase):
    def test_build_content_keeps_scheduled_reminder_guardrails(self) -> None:
        adapter = PicoClawAdapter(
            PicoBridgeConfig(
                url="ws://127.0.0.1:18790/pico/ws",
                token="token",
                session_id="voice-pet-test",
                timeout_seconds=30,
                node_script="/tmp/pico_bridge_session.js",
            )
        )

        content = adapter._build_content("一分钟后提醒我喝水")

        self.assertIn("一分钟后提醒我喝水", content)
        self.assertIn("定时", content)
        self.assertIn("只创建提醒", content)

    def test_reply_with_cancel_restarts_sidecar(self) -> None:
        adapter = PicoClawAdapter(
            PicoBridgeConfig(
                url="ws://127.0.0.1:18790/pico/ws",
                token="token",
                session_id="voice-pet-test",
                timeout_seconds=30,
                node_script="/tmp/pico_bridge_session.js",
            )
        )
        cancel_event = Event()
        sidecar = _FakeSidecar(cancel_event)
        adapter._sidecar = sidecar

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            adapter.reply_with_cancel("慢任务", cancel_event)

        self.assertEqual(sidecar.restart_calls, 1)
        self.assertTrue(sidecar.sent_payloads)
        self.assertIn("慢任务", sidecar.sent_payloads[0][1])

    def test_push_message_runs_callback(self) -> None:
        pushed: list[str] = []
        sidecar = _FakeSidecar(Event())
        sidecar.on_push_message = pushed.append

        sidecar._handle_event({"type": "push", "text": "主人，到时间喝水啦。"})

        self.assertEqual(pushed, ["主人，到时间喝水啦。"])

    def test_reply_reads_sidecar_response(self) -> None:
        adapter = PicoClawAdapter(
            PicoBridgeConfig(
                url="ws://127.0.0.1:18790/pico/ws",
                token="token",
                session_id="voice-pet-test",
                timeout_seconds=30,
                node_script="/tmp/pico_bridge_session.js",
            )
        )
        sidecar = _ImmediateReplySidecar()
        adapter._sidecar = sidecar

        reply = adapter.reply("今天天气怎么样")

        self.assertEqual(reply, "今天天气晴。")
        self.assertEqual(len(sidecar.requests), 1)
        self.assertIn("今天天气怎么样", sidecar.requests[0])


class _FakeSidecar:
    def __init__(self, cancel_event: Event):
        self.cancel_event = cancel_event
        self.restart_calls = 0
        self.sent_payloads: list[tuple[str, str]] = []
        self.on_push_message = None

    def send_request(self, request_id: str, text: str) -> None:
        self.sent_payloads.append((request_id, text))
        self.cancel_event.set()

    def await_reply(self, request_id: str, timeout_seconds: float) -> str:
        raise queue.Empty

    def restart(self) -> None:
        self.restart_calls += 1

    def request(self, text: str) -> str:
        raise NotImplementedError

    def _handle_event(self, payload: dict[str, object]) -> None:
        event_type = str(payload.get("type", "")).strip()
        if event_type != "push":
            return
        text = str(payload.get("text", "")).strip()
        if text and self.on_push_message is not None:
            self.on_push_message(text)


class _ImmediateReplySidecar:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def request(self, text: str) -> str:
        self.requests.append(text)
        return "今天天气晴。"


if __name__ == "__main__":
    unittest.main()
