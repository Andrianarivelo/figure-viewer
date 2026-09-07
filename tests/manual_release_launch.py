"""Verify normal frozen launch, startup ownership and Explorer-style handoffs.

Usage: python tests/manual_release_launch.py --output REPORT.json EXE [EXE ...]
Child processes receive a disposable Windows profile and a Windows-only PATH.
Only windows owned by the isolated instance are closed. This is a manual
release integration check, intentionally excluded from unittest discovery.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import traceback

from PIL import Image

LAUNCH_TIMEOUT = 50.0
CLOSE_TIMEOUT = 10.0
WM_CLOSE = 0x0010


def windows_for_process(pid: int) -> list[tuple[int, str]]:
    """Read native top-level titles belonging exclusively to one known PID."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    result = []

    @callback_type
    def collect(hwnd, unused):
        """Retain titled windows of the isolated application process only."""
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if title.value.startswith("FigView - "):
                result.append((hwnd, title.value))
        return True

    user32.EnumWindows(collect, 0)
    return result


def close_windows(pid: int) -> None:
    """Request normal shutdown from test-owned viewer windows."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for hwnd, title in windows_for_process(pid):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def verify(executable: Path) -> dict:
    """Exercise two simultaneous cold launches and a third warm handoff."""
    started = time.monotonic()
    result = {"executable": str(executable), "ok": False}
    processes = []
    owner_pid = None
    with tempfile.TemporaryDirectory(prefix="figview-normal-launch-") as directory:
        workspace = Path(directory)
        profile = workspace / "profile"
        profile.mkdir()
        settings = profile / "AppData" / "Roaming" / "FigureViewerApp"
        instance_path = settings / "instance.json"
        environment = os.environ.copy()
        environment.update({
            "USERPROFILE": str(profile), "HOME": str(profile),
            "HOMEDRIVE": profile.drive, "HOMEPATH": str(profile)[len(profile.drive):],
            "APPDATA": str(profile / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(profile / "AppData" / "Local"),
            "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
        })
        # Native frozen imports must not accidentally resolve through Python.
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
            environment.pop(name, None)
        files = [workspace / f"handoff-{index}.png" for index in range(3)]
        for index, path in enumerate(files):
            Image.new("RGB", (80, 60), (40 + 20 * index, 80, 120)).save(path)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0

        def launch(path):
            """Start only the supplied executable, with no shell or Python."""
            process = subprocess.Popen([str(executable), str(path)], cwd=workspace,
                                       env=environment, startupinfo=startup)
            processes.append(process)
            return process

        def wait_until(predicate):
            """Bound all startup and handoff waits with one shared deadline."""
            while time.monotonic() - started < LAUNCH_TIMEOUT:
                if predicate():
                    return
                time.sleep(0.05)
            raise TimeoutError("Normal executable launch/handoff exceeded 50 seconds")

        try:
            launch(files[0])
            launch(files[1])
            wait_until(instance_path.exists)
            # Publication can briefly expose a partially written JSON file.
            def read_owner():
                """Capture the PID advertised only within the test profile."""
                nonlocal owner_pid
                try:
                    owner_pid = int(json.loads(instance_path.read_text())["pid"])
                    return True
                except (ValueError, OSError, KeyError):
                    return False

            wait_until(read_owner)
            wait_until(lambda: sum(process.poll() is not None for process in processes) == 1)
            exited = [process.returncode for process in processes if process.poll() is not None]
            if exited != [0]:
                raise RuntimeError(f"Cold follower exit codes: {exited}")
            wait_until(lambda: len(windows_for_process(owner_pid)) == 1)
            result["cold_start_seconds"] = round(time.monotonic() - started, 3)
            third_started = time.monotonic()
            third = launch(files[2])
            wait_until(lambda: third.poll() is not None)
            if third.returncode != 0:
                raise RuntimeError(f"Warm handoff exited {third.returncode}")
            wait_until(lambda: any(files[2].name in title for _, title in windows_for_process(owner_pid)))
            titles = windows_for_process(owner_pid)
            if len(titles) != 1:
                raise RuntimeError(f"Expected one viewer, got {titles}")
            result.update({"warm_handoff_seconds": round(time.monotonic() - third_started, 3),
                           "owner_pid": owner_pid, "viewer_windows": len(titles),
                           "final_title": titles[0][1]})
            close_windows(owner_pid)
            for process in processes:
                if process.wait(timeout=CLOSE_TIMEOUT) != 0:
                    raise RuntimeError("Executable did not exit cleanly")
            saved = json.loads((settings / "settings.json").read_text(encoding="utf-8"))
            recent = {os.path.normcase(str(Path(path))) for path in saved["recent_files"]}
            if not {os.path.normcase(str(path)) for path in files}.issubset(recent):
                raise RuntimeError(f"A cold-start or warm-handoff file was lost: {recent}")
            if instance_path.exists():
                raise RuntimeError("Instance advertisement survived normal shutdown")
            result.update({"ok": True, "files_preserved": len(files),
                           "exit_codes": [process.returncode for process in processes]})
        except Exception:
            result["error"] = traceback.format_exc()
        finally:
            if owner_pid:
                close_windows(owner_pid)
            for process in processes:
                if process.poll() is None:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # /T includes a one-file bootloader's own child only.
                        subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                                        "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, timeout=5)
                        process.wait(timeout=5)
            result["seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> int:
    """Write release QA evidence suitable for archiving beside build reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("executables", type=Path, nargs="+")
    arguments = parser.parse_args()
    results = [verify(path.resolve(strict=True)) for path in arguments.executables]
    report = {"ok": all(result["ok"] for result in results), "results": results}
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
