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


def launch_browser(browser_path: Path, url: str, profile_dir: Path) -> subprocess.Popen[str]:
    browser_name = browser_path.name.lower()
    common_flags = ["--no-first-run", "--disable-extensions"]

    if browser_name == "firefox.exe":
        command = [
            str(browser_path),
            "-new-instance",
            "-profile",
            str(profile_dir),
            url,
        ]
    else:
        command = [
            str(browser_path),
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
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
    url = f"http://{host}:{port}"
    browser_profile_dir = Path(tempfile.mkdtemp(prefix="buen_yantar_browser_"))

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
        browser_process.wait()
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
        shutil.rmtree(browser_profile_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
