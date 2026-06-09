from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

VOICE_REPLY_PROMPT = (
    "你是小爱，一个本地桌面语音助手，请用自然口语中文回答。"
    "不要自称 PicoClaw、Pico、模型或 AI。"
    "回答要短，但必须有信息量，不能用空泛话糊弄。"
    "简单问题用 1 到 2 句话；新闻、天气、整理、对比、步骤类问题可以给 3 到 5 个短要点。"
    "不要使用 markdown、emoji、代码块；可以用顿号、分号或一二三这种口语编号。"
    "点歌或播放音乐时，只说正在播放哪首歌和必要状态，不要输出任何链接、URL 或网页地址。"
    "如果用户说一分钟后、十分钟后、稍后、之后、定时、提醒、闹钟或倒计时播放某首歌，"
    "这是定时提醒/计划任务，不是现在点歌；不要立刻调用音乐、网易云、ncm 或播放工具，"
    "只创建提醒或说明会到时提醒。"
)


@dataclass(slots=True)
class PicoBridgeConfig:
    url: str
    token: str
    session_id: str = "voice-pet"
    timeout_seconds: float = 30.0
    node_script: str = "~/.picoclaw/voice-pet/pico_bridge_session.js"
    progress_path: str = ""


class PicoClawAdapter:
    def __init__(self, cfg: PicoBridgeConfig, on_push_message: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.on_push_message = on_push_message
        self._sidecar: _PicoBridgeSidecar | None = None
        self._sidecar_lock = threading.Lock()

    def reply(self, text: str) -> str:
        return self._sidecar_client().request(self._build_content(text))

    def reply_with_cancel(self, text: str, cancel_event: Event) -> str:
        sidecar = self._sidecar_client()
        request_id = uuid.uuid4().hex
        sidecar.send_request(request_id, self._build_content(text))
        deadline = time.monotonic() + max(int(self.cfg.timeout_seconds) + 5, 10)
        while True:
            if cancel_event.is_set():
                sidecar.restart()
                print("[picoclaw] reply_cancelled")
                raise RuntimeError("PicoClaw reply cancelled")
            timeout_seconds = min(0.1, max(0.0, deadline - time.monotonic()))
            try:
                return sidecar.await_reply(request_id, timeout_seconds)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timeout waiting for PicoClaw reply")
                continue

    def close(self) -> None:
        with self._sidecar_lock:
            if self._sidecar is not None:
                self._sidecar.close()
                self._sidecar = None

    def _sidecar_client(self) -> "_PicoBridgeSidecar":
        with self._sidecar_lock:
            if self._sidecar is None:
                self._sidecar = _PicoBridgeSidecar(self.cfg, self.on_push_message)
            return self._sidecar

    def _build_content(self, text: str) -> str:
        return f"{VOICE_REPLY_PROMPT}\n\n用户问题：{text}"


class _PicoBridgeSidecar:
    def __init__(self, cfg: PicoBridgeConfig, on_push_message: Callable[[str], None] | None):
        self.cfg = cfg
        self.on_push_message = on_push_message
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._reply_queues: dict[str, queue.Queue[dict[str, str]]] = {}
        self._progress_path = Path(cfg.progress_path).expanduser() if cfg.progress_path else None
        self._start_process()

    def request(self, text: str) -> str:
        request_id = uuid.uuid4().hex
        self.send_request(request_id, text)
        timeout_seconds = max(int(self.cfg.timeout_seconds) + 5, 10)
        return self.await_reply(request_id, timeout_seconds)

    def send_request(self, request_id: str, text: str) -> None:
        self._reply_queues[request_id] = queue.Queue(maxsize=1)
        payload = {
            "action": "send",
            "request_id": request_id,
            "content": text,
            "timeout_seconds": self.cfg.timeout_seconds,
        }
        try:
            self._send_json(payload)
        except Exception:
            self._reply_queues.pop(request_id, None)
            raise

    def await_reply(self, request_id: str, timeout_seconds: float) -> str:
        reply_queue = self._reply_queues.get(request_id)
        if reply_queue is None:
            raise RuntimeError(f"missing reply queue for request {request_id}")
        try:
            result = reply_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            raise
        else:
            self._reply_queues.pop(request_id, None)
        kind = str(result.get("type", "")).strip()
        if kind == "reply":
            return _validated_reply(str(result.get("text", "")).strip())
        message = str(result.get("message", "")).strip() or "unknown PicoClaw bridge error"
        raise RuntimeError(message)

    def restart(self) -> None:
        with self._lifecycle_lock:
            self._stop_process()
            self._start_process()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._stop_process()

    def _start_process(self) -> None:
        script = str(Path(self.cfg.node_script).expanduser())
        cmd = [
            "node",
            script,
            self.cfg.url,
            self.cfg.token,
            self.cfg.session_id,
            str(self.cfg.timeout_seconds),
            str(self._progress_path) if self._progress_path else "",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _stop_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self._clear_progress()
        for request_id, reply_queue in list(self._reply_queues.items()):
            try:
                reply_queue.put_nowait(
                    {
                        "type": "error",
                        "message": "pico bridge sidecar stopped while waiting for reply",
                    }
                )
            except queue.Full:
                pass

    def _send_json(self, payload: dict[str, str]) -> None:
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise RuntimeError("pico bridge sidecar is not running")
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def _read_stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[picoclaw] invalid_sidecar_stdout={raw}")
                continue
            self._handle_event(payload)

    def _read_stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            text = line.strip()
            if text:
                print(f"[picoclaw] sidecar={text}")

    def _handle_event(self, payload: dict[str, object]) -> None:
        event_type = str(payload.get("type", "")).strip()
        if event_type == "reply":
            request_id = str(payload.get("request_id", "")).strip()
            reply = str(payload.get("text", "") or payload.get("content", "")).strip()
            reply_queue = self._reply_queues.get(request_id)
            if reply_queue is not None:
                try:
                    reply_queue.put_nowait({"type": "reply", "text": reply})
                except queue.Full:
                    pass
            return
        if event_type == "progress":
            self._write_progress(payload)
            return
        if event_type == "push":
            self._clear_progress()
            content = str(payload.get("text", "") or payload.get("content", "")).strip()
            if content and self.on_push_message is not None:
                self.on_push_message(content)
            return
        if event_type == "error":
            self._clear_progress()
            request_id = str(payload.get("request_id", "")).strip()
            message = str(payload.get("message", "") or payload.get("error", "")).strip() or "unknown PicoClaw bridge error"
            reply_queue = self._reply_queues.get(request_id)
            if reply_queue is not None:
                try:
                    reply_queue.put_nowait({"type": "error", "message": message})
                except queue.Full:
                    pass

    def _write_progress(self, payload: dict[str, object]) -> None:
        if self._progress_path is None:
            return
        text = str(payload.get("text", "")).strip()
        if not text:
            return
        body = {
            "text": text,
            "kind": str(payload.get("kind", "")).strip() or "tool_calls",
            "updated_at": time.time(),
        }
        try:
            self._progress_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._progress_path.with_suffix(self._progress_path.suffix + f".{uuid.uuid4().hex}.tmp")
            tmp_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self._progress_path)
        except OSError as exc:
            print(f"[picoclaw] progress_write_failed={exc}")

    def _clear_progress(self) -> None:
        if self._progress_path is None:
            return
        try:
            self._progress_path.unlink(missing_ok=True)
        except OSError:
            pass


def _validated_reply(text: str) -> str:
    reply = text.strip()
    if not reply:
        raise RuntimeError("empty PicoClaw reply")
    return reply
