from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PicoClawGatewayProcess:
    def __init__(self, runtime: dict[str, Any]):
        self.enabled = bool(runtime.get("picoclaw_manage_gateway", False))
        self.command = str(runtime.get("picoclaw_gateway_command", "picoclaw")).strip()
        self.args = _args_list(runtime.get("picoclaw_gateway_args", ["gateway"]))
        self.cwd = str(runtime.get("picoclaw_gateway_cwd", "")).strip()
        self.ready_url = str(runtime.get("picoclaw_gateway_ready_url", "http://127.0.0.1:18790/ready")).strip()
        self.start_timeout_seconds = float(runtime.get("picoclaw_gateway_start_timeout_seconds", 20.0))
        self.stop_timeout_seconds = float(runtime.get("picoclaw_gateway_stop_timeout_seconds", 10.0))
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        if self._is_ready():
            print(f"[picoclaw] gateway already ready: {self.ready_url}")
            return
        if not self.command:
            raise ValueError("runtime.picoclaw_gateway_command is empty")

        cmd = [self.command, *self.args]
        cwd = str(Path(self.cwd).expanduser()) if self.cwd else None
        print(f"[picoclaw] starting gateway: {' '.join(shlex.quote(part) for part in cmd)}")
        self.proc = subprocess.Popen(cmd, cwd=cwd)
        try:
            self._wait_ready()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return

        print("[picoclaw] stopping managed gateway")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=self.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        finally:
            self.proc = None

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.start_timeout_seconds
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"PicoClaw gateway exited early with code {self.proc.returncode}")
            if self._is_ready():
                print(f"[picoclaw] gateway ready: {self.ready_url}")
                return
            time.sleep(0.5)
        raise TimeoutError(f"PicoClaw gateway did not become ready within {self.start_timeout_seconds:.1f}s")

    def _is_ready(self) -> bool:
        if not self.ready_url:
            return False
        req = Request(self.ready_url, method="GET")
        try:
            with urlopen(req, timeout=1) as resp:
                return 200 <= resp.status < 300
        except HTTPError:
            return False
        except (OSError, URLError):
            return False


def _args_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return shlex.split(os.path.expandvars(os.path.expanduser(value)))
    if isinstance(value, list):
        return [str(item) for item in value]
    return []
