"""Lightweight shell-open forwarding without importing GUI or image libraries."""
from __future__ import annotations

import json
import socket
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

SETTINGS_DIR = Path.home() / "AppData" / "Roaming" / "FigureViewerApp"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
INSTANCE_PATH = SETTINGS_DIR / "instance.json"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
STARTUP_LOCK_PATH = SETTINGS_DIR / "startup.lock"
STARTUP_WAIT_SECONDS = 15.0


def read_message(conn: socket.socket) -> dict:
    """Read a bounded JSON object, accepting old clients without a newline.

    TCP may split any send across several receives. Parse accumulated bytes
    instead of assuming one receive contains the entire selection of files.
    """
    chunks = bytearray()
    while len(chunks) < MAX_MESSAGE_BYTES:
        block = conn.recv(min(65536, MAX_MESSAGE_BYTES - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        try:
            message = json.loads(chunks.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            raise ValueError("Expected an IPC object")
        return message
    raise ValueError("Incomplete or oversized IPC message")


#: Command-line flag of the Explorer "Combine with FigView" verb: the PDFs
#: are collected by the running instance and offered for combining instead
#: of being opened.
COMBINE_FLAG = "--combine"


def hand_over_to_running_instance(
    paths: Iterable[Path], *, instance_path: Path = INSTANCE_PATH,
    combine: bool = False,
) -> bool:
    """Send absolute paths and return true only after a positive acknowledgment.

    ``combine`` marks the paths as a combine request (see :data:`COMBINE_FLAG`).
    """
    try:
        stored = json.loads(instance_path.read_text(encoding="utf-8"))
        message = {
            "token": str(stored["token"]),
            "paths": [str(path.expanduser().absolute()) for path in paths],
        }
        if combine:
            message["combine"] = True
        payload = json.dumps(message).encode("utf-8") + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            return False
        with socket.create_connection(("127.0.0.1", int(stored["port"])), timeout=1.5) as conn:
            conn.settimeout(1.5)
            conn.sendall(payload)
            return read_message(conn).get("ok") is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def forward_arguments(arguments: Iterable[str]) -> bool:
    """Take the fast shell-open path only when saved single-instance mode allows it."""
    arguments = list(arguments)
    paths = [Path(item) for item in arguments if item and not item.startswith("-")]
    if not paths or not INSTANCE_PATH.is_file():
        return False
    if not single_instance_enabled():
        return False
    return hand_over_to_running_instance(paths, instance_path=INSTANCE_PATH,
                                         combine=COMBINE_FLAG in arguments)


def single_instance_enabled() -> bool:
    """Read only the startup setting, without importing the application."""
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        settings = {}
    return not isinstance(settings, dict) or bool(settings.get("single_instance", True))


def _try_lock(stream) -> bool:
    """Claim one OS-managed byte lock, automatically released after a crash."""
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@contextmanager
def startup_claim(arguments: Iterable[str]):
    """Serialize cold shell-open starts until the elected viewer accepts files.

    Keep the OS lock for the viewer lifetime. Waiting launchers repeatedly try
    IPC as the owner initializes, and acquire the lock if it exits or crashes.
    An empty shortcut launch retains the existing new-window behavior.
    """
    arguments = list(arguments)
    if not any(item and not item.startswith("-") for item in arguments) or not single_instance_enabled():
        yield False
        return
    try:
        STARTUP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Never delete this stable inode: concurrent launchers must lock the
        # same file. The operating system owns lock lifetime, not file content.
        stream = STARTUP_LOCK_PATH.open("a+b")
    except OSError:
        yield False
        return
    with stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + STARTUP_WAIT_SECONDS
        while True:
            if forward_arguments(arguments):
                yield True
                return
            if _try_lock(stream):
                # Closing the stream releases the OS lock, including when
                # dependency imports or GUI initialization raise an exception.
                yield False
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("The viewer is still starting. Please try opening the files again.")
            time.sleep(0.05)
