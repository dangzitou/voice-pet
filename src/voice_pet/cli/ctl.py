from __future__ import annotations

import argparse
import http.client
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

from ..config import load_config


DEFAULT_CONFIG_PATH = "~/.picoclaw/voice-pet/config.json"
DEFAULT_ENV_PATH = "~/.picoclaw/voice-pet/voice-pet.env"
PROXY_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local voice-pet runtime")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"config path, default: {DEFAULT_CONFIG_PATH}")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=f"env file path, default: {DEFAULT_ENV_PATH}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="start PicoClaw gateway and voice-pet, then follow logs")
    start_parser.add_argument("--no-gateway", action="store_true", help="start only voice-pet")
    start_parser.add_argument("--detach", action="store_true", help="start in the background without following logs")
    start_parser.add_argument("-n", "--lines", type=int, default=80, help="initial log lines to print before following")
    start_parser.set_defaults(func=_cmd_start)

    stop_parser = subparsers.add_parser("stop", help="stop voice-pet and PicoClaw gateway")
    stop_parser.add_argument("--no-gateway", action="store_true", help="stop only voice-pet")
    stop_parser.set_defaults(func=_cmd_stop)

    restart_parser = subparsers.add_parser("restart", help="restart PicoClaw gateway and voice-pet")
    restart_parser.set_defaults(func=_cmd_restart)

    status_parser = subparsers.add_parser("status", help="show process and gateway status")
    status_parser.set_defaults(func=_cmd_status)

    logs_parser = subparsers.add_parser("logs", help="print recent logs")
    logs_parser.add_argument("--target", choices=("voice", "gateway"), default="voice")
    logs_parser.add_argument("-n", "--lines", type=int, default=80)
    logs_parser.add_argument("-f", "--follow", action="store_true")
    logs_parser.set_defaults(func=_cmd_logs)

    config_parser = subparsers.add_parser("config", help="run the configuration CLI")
    config_parser.set_defaults(func=_cmd_config)

    mock_parser = subparsers.add_parser("mock", help="run the mock end-to-end test")
    mock_parser.set_defaults(func=_cmd_mock)

    demo_parser = subparsers.add_parser("demo", help="run the MiMo TTS / ASR demo")
    demo_parser.set_defaults(func=_cmd_demo)

    args, passthrough = parser.parse_known_args()
    args.args = passthrough
    if passthrough and args.command not in {"config", "mock", "demo"}:
        parser.error(f"unrecognized arguments: {' '.join(passthrough)}")
    args.func(args)


def _cmd_start(args: argparse.Namespace) -> None:
    env_path = Path(args.env).expanduser()
    _load_env_file_into_process(env_path)
    config = load_config(args.config)
    paths = _runtime_paths(config)
    env = _child_env(paths.repo_root, env_path)

    if not args.no_gateway:
        _start_gateway(config, paths, env)
    _start_voice(args.config, paths, env)
    _print_status(config, paths)
    if not args.detach:
        print("")
        print(f"following voice-pet logs; press Ctrl+C to stop following, services keep running: {paths.voice_log}")
        _follow_log(paths.voice_log, args.lines)


def _cmd_stop(args: argparse.Namespace) -> None:
    _load_env_file_into_process(Path(args.env).expanduser())
    config = load_config(args.config)
    paths = _runtime_paths(config)

    voice_pids = _unique_pids(_pid_from_file(paths.voice_pid), *_find_voice_pids())
    _terminate_pids("voice-pet", voice_pids)
    if not args.no_gateway:
        gateway_pids = _unique_pids(_pid_from_file(paths.gateway_pid), *_find_gateway_pids())
        _terminate_pids("PicoClaw gateway", gateway_pids)


def _cmd_restart(args: argparse.Namespace) -> None:
    stop_args = argparse.Namespace(config=args.config, env=args.env, no_gateway=False)
    start_args = argparse.Namespace(config=args.config, env=args.env, no_gateway=False, detach=True, lines=80)
    _cmd_stop(stop_args)
    time.sleep(0.5)
    _cmd_start(start_args)


def _cmd_status(args: argparse.Namespace) -> None:
    _load_env_file_into_process(Path(args.env).expanduser())
    config = load_config(args.config)
    paths = _runtime_paths(config)
    _print_status(config, paths)


def _cmd_logs(args: argparse.Namespace) -> None:
    _load_env_file_into_process(Path(args.env).expanduser())
    config = load_config(args.config)
    paths = _runtime_paths(config)
    log_path = paths.voice_log if args.target == "voice" else paths.gateway_log
    if args.follow:
        _follow_log(log_path, args.lines)
        return
    subprocess.run(["tail", "-n", str(args.lines), str(log_path)], check=False)


def _cmd_config(args: argparse.Namespace) -> None:
    _run_tool_module("voice_pet.config_cli", args)


def _cmd_mock(args: argparse.Namespace) -> None:
    _run_tool_module("voice_pet.mock_mvp", args)


def _cmd_demo(args: argparse.Namespace) -> None:
    _run_tool_module("voice_pet.demo_loop", args)


class RuntimePaths:
    def __init__(self, config: dict[str, Any]):
        self.repo_root = Path(__file__).resolve().parents[3]
        runtime = config.get("runtime", {})
        work_dir = Path(str(runtime.get("work_dir", "~/.picoclaw/voice-pet/runtime"))).expanduser()
        work_dir.mkdir(parents=True, exist_ok=True)
        gateway_log_dir = Path("~/.picoclaw/logs").expanduser()
        gateway_log_dir.mkdir(parents=True, exist_ok=True)

        self.work_dir = work_dir
        self.voice_pid = work_dir / "voice-pet.pid"
        self.gateway_pid = work_dir / "gateway.pid"
        self.voice_log = work_dir / "voice-pet.log"
        self.gateway_log = gateway_log_dir / "gateway-voice-pet.log"


def _runtime_paths(config: dict[str, Any]) -> RuntimePaths:
    return RuntimePaths(config)


def _start_gateway(config: dict[str, Any], paths: RuntimePaths, env: dict[str, str]) -> None:
    health_url = _gateway_health_url(config)
    if _health_ok(health_url, timeout=1.0):
        print(f"gateway already healthy: {health_url}")
        return

    existing = _find_gateway_pids()
    if existing:
        print(f"gateway process exists but health check is not OK: {existing}")
        return

    cmd = _gateway_command(config, paths.repo_root)
    print(f"starting gateway: {_quote_cmd(cmd)}")
    pid = _spawn_background(cmd, paths.gateway_log, env, cwd=paths.repo_root.parent / "picoclaw")
    _write_pid(paths.gateway_pid, pid)

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if _health_ok(health_url, timeout=1.0):
            print(f"gateway ready: {health_url}")
            return
        time.sleep(0.3)
    print(f"gateway started but health is not ready yet: pid={pid}, log={paths.gateway_log}")


def _start_voice(config_path: str, paths: RuntimePaths, env: dict[str, str]) -> None:
    existing = _find_voice_pids()
    if existing:
        print(f"voice-pet already running: {existing}")
        return

    cmd = [
        sys.executable,
        "-m",
        "voice_pet.main",
        "--config",
        str(Path(config_path).expanduser()),
    ]
    print(f"starting voice-pet: {_quote_cmd(cmd)}")
    pid = _spawn_background(cmd, paths.voice_log, env, cwd=paths.repo_root)
    _write_pid(paths.voice_pid, pid)
    time.sleep(0.8)
    if _pid_alive(pid):
        print(f"voice-pet started: pid={pid}, log={paths.voice_log}")
    else:
        print(f"voice-pet exited early, check log: {paths.voice_log}")


def _gateway_command(config: dict[str, Any], repo_root: Path) -> list[str]:
    runtime = config.get("runtime", {})
    raw_command = str(runtime.get("picoclaw_gateway_command", "picoclaw")).strip() or "picoclaw"
    command = _resolve_gateway_command(raw_command, repo_root)
    args = _args_list(runtime.get("picoclaw_gateway_args", ["gateway", "-E", "--host", "127.0.0.1"]))
    if args == ["gateway"]:
        args = ["gateway", "-E", "--host", "127.0.0.1"]
    return [command, *args]


def _resolve_gateway_command(command: str, repo_root: Path) -> str:
    if "/" in command:
        return str(Path(command).expanduser())
    found = shutil.which(command)
    if found:
        return found
    sibling = repo_root.parent / "picoclaw" / "build" / "picoclaw"
    if sibling.exists():
        return str(sibling)
    return command


def _args_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return shlex.split(value)
    return []


def _spawn_background(cmd: list[str], log_path: Path, env: dict[str, str], cwd: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab", buffering=0)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd.exists() else None,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        return proc.pid
    finally:
        log_file.close()


def _follow_log(log_path: Path, lines: int) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    try:
        subprocess.run(["tail", "-n", str(lines), "-F", str(log_path)], check=False)
    except KeyboardInterrupt:
        print("\nstopped following logs; services keep running")


def _print_status(config: dict[str, Any], paths: RuntimePaths) -> None:
    gateway_pids = _unique_pids(_pid_from_file(paths.gateway_pid), *_find_gateway_pids())
    voice_pids = _unique_pids(_pid_from_file(paths.voice_pid), *_find_voice_pids())
    health_url = _gateway_health_url(config)
    health = "ok" if _health_ok(health_url, timeout=1.0) else "not ok"

    print(f"gateway: {gateway_pids or 'stopped'} ({health})")
    print(f"voice-pet: {voice_pids or 'stopped'}")
    print(f"voice log: {paths.voice_log}")
    print(f"gateway log: {paths.gateway_log}")


def _gateway_health_url(config: dict[str, Any]) -> str:
    runtime = config.get("runtime", {})
    ready_url = str(runtime.get("picoclaw_gateway_ready_url", "http://127.0.0.1:18790/ready"))
    parsed = urlparse(ready_url)
    if not parsed.scheme or not parsed.netloc:
        return "http://127.0.0.1:18790/health"
    return f"{parsed.scheme}://{parsed.netloc}/health"


def _health_ok(url: str, timeout: float) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    try:
        conn.request("GET", parsed.path or "/health")
        resp = conn.getresponse()
        resp.read()
        return 200 <= resp.status < 300
    except OSError:
        return False
    finally:
        conn.close()


def _load_env_file_into_process(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ[key] = value


def _child_env(repo_root: Path, env_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    _load_env_file_into_mapping(env_path, env)
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    src_path = str(repo_root / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_path if not existing else f"{src_path}:{existing}"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _load_env_file_into_mapping(path: Path, env: dict[str, str]) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            env[key] = value


def _find_voice_pids() -> list[int]:
    return [
        pid for pid, cmdline in _iter_processes()
        if "voice_pet.main" in cmdline and pid != os.getpid()
    ]


def _find_gateway_pids() -> list[int]:
    return [
        pid for pid, cmdline in _iter_processes()
        if "picoclaw" in cmdline and "gateway" in cmdline and pid != os.getpid()
    ]


def _iter_processes() -> list[tuple[int, str]]:
    processes: list[tuple[int, str]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return processes
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
        processes.append((int(entry.name), cmdline))
    return processes


def _terminate_pids(label: str, pids: list[int]) -> None:
    if not pids:
        print(f"{label}: already stopped")
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            print(f"{label}: stopped {pids}")
            return
        time.sleep(0.2)
    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    print(f"{label}: killed {pids}")


def _pid_from_file(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not value.isdigit():
        return None
    pid = int(value)
    return pid if _pid_alive(pid) else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def _unique_pids(*pids: int | None) -> list[int]:
    seen = set()
    result = []
    for pid in pids:
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
    return result


def _quote_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def _run_tool_module(module: str, args: argparse.Namespace) -> None:
    env_path = Path(args.env).expanduser()
    _load_env_file_into_process(env_path)
    paths = _runtime_paths(load_config(args.config))
    env = _child_env(paths.repo_root, env_path)
    cmd = [sys.executable, "-m", module]
    if args.config != DEFAULT_CONFIG_PATH and "--config" not in args.args:
        cmd.extend(["--config", args.config])
    cmd.extend(args.args)
    raise SystemExit(subprocess.run(cmd, cwd=str(paths.repo_root), env=env, check=False).returncode)


if __name__ == "__main__":
    main()
