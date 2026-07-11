from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import ctypes
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl


ROOT_DIR = Path(__file__).resolve().parent
SW_MAXIMIZE = 3


def env_with_defaults() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("LOW_RESOURCE_MODE", "1")
    env.setdefault("WAITRESS_THREADS", "2")
    env.setdefault("WAITRESS_CONNECTION_LIMIT", "40")
    env.setdefault("HOST", "127.0.0.1")
    env.setdefault("PORT", "8080")
    return env


def launcher_browser_profile_dir() -> Path:
    override = os.getenv("BROWSER_PROFILE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return ROOT_DIR / "data" / "launcher_browser_profile"


def wait_for_server(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"El servidor no respondió en {host}:{port} tras {timeout:.0f} segundos.")


def browser_candidates() -> list[Path]:
    paths: list[Path] = []
    browser_override = os.getenv("BROWSER_PATH", "").strip()
    if browser_override:
        paths.append(Path(browser_override))

    program_files = [
        Path(os.getenv("PROGRAMFILES", "")),
        Path(os.getenv("PROGRAMFILES(X86)", "")),
        Path(os.getenv("LOCALAPPDATA", "")),
    ]
    relative_candidates = [
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Chromium/Application/chrome.exe"),
        Path("BraveSoftware/Brave-Browser/Application/brave.exe"),
        Path("Mozilla Firefox/firefox.exe"),
    ]

    for base in program_files:
        if not str(base):
            continue
        for relative in relative_candidates:
            paths.append(base / relative)
    return paths


def resolve_browser() -> Path:
    for candidate in browser_candidates():
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "No se encontró un navegador compatible. Instala Google Chrome o Firefox, "
        "o define la variable BROWSER_PATH apuntando al ejecutable."
    )


def launcher_target_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["launcher"] = "1"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

def launch_browser(browser_path: Path, target_url: str, profile_dir: Path) -> subprocess.Popen[str]:
    browser_name = browser_path.name.lower()
    common_flags = ["--no-first-run", "--disable-extensions"]

    if browser_name == "firefox.exe":
        command = [
            str(browser_path),
            "-new-instance",
            "-new-window",
            "-profile",
            str(profile_dir),
            target_url,
        ]
    else:
        command = [
            str(browser_path),
            f"--app={target_url}",
            f"--user-data-dir={profile_dir}",
            "--start-fullscreen",
            "--start-maximized",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-sync",
            "--no-default-browser-check",
            *common_flags,
        ]
    return subprocess.Popen(command, cwd=ROOT_DIR)


def maximize_browser_window(process: subprocess.Popen[str], timeout: float = 8.0) -> None:
    if os.name != "nt":
        return

    user32 = ctypes.windll.user32
    target_pid = process.pid
    deadline = time.time() + timeout

    while time.time() < deadline and process.poll() is None:
        found_hwnd = None

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_windows_proc(hwnd, _lparam):
            nonlocal found_hwnd
            if not user32.IsWindowVisible(hwnd):
                return True

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != target_pid:
                return True

            found_hwnd = hwnd
            return False

        user32.EnumWindows(enum_windows_proc, 0)
        if found_hwnd:
            user32.ShowWindow(found_hwnd, SW_MAXIMIZE)
            user32.SetForegroundWindow(found_hwnd)
            return

        time.sleep(0.2)


def stop_process(process: subprocess.Popen[str], timeout: float = 8.0) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, min(timeout, 5.0)),
            )
        except Exception:
            pass

    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    env = env_with_defaults()
    host = env["HOST"]
    port = int(env["PORT"])
    url = launcher_target_url(f"http://{host}:{port}")
    launcher_dir = Path(tempfile.mkdtemp(prefix="buen_yantar_launcher_"))
    browser_profile_dir = launcher_browser_profile_dir()
    browser_profile_dir.mkdir(parents=True, exist_ok=True)
    exit_signal_path = launcher_dir / "close.signal"
    env["LAUNCHER_EXIT_SIGNAL"] = str(exit_signal_path)

    server_process = subprocess.Popen(
        [sys.executable, "run_production.py"],
        cwd=ROOT_DIR,
        env=env,
    )

    browser_process: subprocess.Popen[str] | None = None
    try:
        wait_for_server(host, port)
        browser_path = resolve_browser()
        browser_process = launch_browser(browser_path, url, browser_profile_dir)
        maximize_browser_window(browser_process)
        while browser_process.poll() is None:
            if exit_signal_path.exists():
                stop_process(browser_process, timeout=4.0)
                return 0
            time.sleep(0.2)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error al iniciar Buen Yantar: {exc}", file=sys.stderr)
        return 1
    finally:
        if browser_process and browser_process.poll() is None:
            stop_process(browser_process, timeout=4.0)
        stop_process(server_process)
        shutil.rmtree(launcher_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
