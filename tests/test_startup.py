"""Regression coverage for shell-open forwarding and dependency startup costs."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import figure_viewer_app
import launch_figure_viewer as launcher
from figureviewer import ipc


class StartupTests(unittest.TestCase):
    def test_package_app_file_routes_through_public_launcher(self):
        """VS Code's Run Python File can start the package file directly."""
        project_dir = Path(__file__).resolve().parent.parent
        script = (
            "import runpy, figure_viewer_app; "
            "figure_viewer_app.main = lambda: 0; "
            "runpy.run_path('figureviewer/app.py', run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=project_dir,
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fragmented_selection_larger_than_one_receive(self):
        """A large Explorer selection survives fragmentation, including UTF-8."""
        payload = {"token": "test", "paths": ["C:/images/é" + str(i) for i in range(6000)]}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        receiver, sender = socket.socketpair()

        def send_fragments():
            with sender:
                for offset in range(0, len(raw), 733):
                    sender.sendall(raw[offset:offset + 733])

        thread = threading.Thread(target=send_fragments)
        thread.start()
        with receiver:
            receiver.settimeout(3)
            self.assertEqual(ipc.read_message(receiver), payload)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_client_requires_positive_acknowledgment(self):
        """An explicit negative reply must not silently discard requested files."""
        with tempfile.TemporaryDirectory() as directory:
            instance_path = Path(directory) / "instance.json"
            instance_path.write_text(json.dumps({"port": 1234, "token": "test"}))
            conn = mock.MagicMock()
            conn.__enter__.return_value = conn
            conn.recv.return_value = b'{"ok": false}'
            with mock.patch.object(ipc.socket, "create_connection", return_value=conn):
                self.assertFalse(ipc.hand_over_to_running_instance(
                    [Path("relative.png")], instance_path=instance_path))
            sent = json.loads(conn.sendall.call_args.args[0])
            self.assertTrue(Path(sent["paths"][0]).is_absolute())

    def test_disabled_single_instance_does_not_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            instance = Path(directory) / "instance.json"
            settings.write_text('{"single_instance": false}')
            instance.write_text('{}')
            with mock.patch.object(ipc, "SETTINGS_PATH", settings), \
                 mock.patch.object(ipc, "INSTANCE_PATH", instance), \
                 mock.patch.object(ipc, "hand_over_to_running_instance") as send:
                self.assertFalse(ipc.forward_arguments(["test.png"]))
                send.assert_not_called()

    def test_bom_settings_preserve_disabled_single_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text('{"single_instance": false}', encoding="utf-8-sig")
            with mock.patch.object(ipc, "SETTINGS_PATH", settings):
                self.assertFalse(ipc.single_instance_enabled())

    def test_launchers_forward_before_gui_startup(self):
        with mock.patch.object(ipc, "forward_arguments", return_value=True), \
             mock.patch.object(launcher, "resolve_python") as resolve:
            self.assertEqual(launcher.main(), 0)
            self.assertEqual(figure_viewer_app.main(), 0)
            resolve.assert_not_called()

    def test_current_interpreter_needs_no_probe_subprocess(self):
        with mock.patch.object(launcher.subprocess, "run") as run:
            self.assertTrue(launcher._can_run_gui(Path(launcher.sys.executable).resolve()))
            run.assert_not_called()

    def test_startup_follower_waits_until_owner_accepts_files(self):
        """A second cold launch hands off as soon as the first publishes IPC."""
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(ipc, "STARTUP_LOCK_PATH", Path(directory) / "startup.lock"), \
             mock.patch.object(ipc, "single_instance_enabled", return_value=True), \
             mock.patch.object(ipc, "forward_arguments", side_effect=[False, True]), \
             mock.patch.object(ipc, "_try_lock", return_value=False), \
             mock.patch.object(ipc.time, "sleep"):
            with ipc.startup_claim(["figure.png"]) as forwarded:
                self.assertTrue(forwarded)

    def test_os_lock_releases_when_owner_exits_context(self):
        """A leftover lock file must never prevent a subsequent viewer launch."""
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(ipc, "STARTUP_LOCK_PATH", Path(directory) / "startup.lock"), \
             mock.patch.object(ipc, "single_instance_enabled", return_value=True), \
             mock.patch.object(ipc, "forward_arguments", return_value=False), \
             mock.patch.object(ipc, "STARTUP_WAIT_SECONDS", 0):
            with ipc.startup_claim(["one.png"]) as forwarded:
                self.assertFalse(forwarded)
                with self.assertRaises(TimeoutError):
                    with ipc.startup_claim(["two.png"]):
                        self.fail("A second process must not start while the owner holds the lock")
            with ipc.startup_claim(["three.png"]) as forwarded:
                self.assertFalse(forwarded)

    def test_startup_lock_releases_after_initialization_failure(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(ipc, "STARTUP_LOCK_PATH", Path(directory) / "startup.lock"), \
             mock.patch.object(ipc, "single_instance_enabled", return_value=True), \
             mock.patch.object(ipc, "forward_arguments", return_value=False), \
             mock.patch.object(ipc, "STARTUP_WAIT_SECONDS", 0):
            with self.assertRaises(RuntimeError):
                with ipc.startup_claim(["one.png"]):
                    raise RuntimeError("GUI initialization failed")
            with ipc.startup_claim(["two.png"]) as forwarded:
                self.assertFalse(forwarded)

    def test_crashed_process_does_not_leave_a_stale_lock(self):
        """Force an unclean process exit, then claim its unchanged lock file."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.lock"
            path.write_bytes(b"0")
            script = (
                "import os,sys; from figureviewer.ipc import _try_lock; "
                "stream=open(sys.argv[1],'a+b'); "
                "assert _try_lock(stream); os._exit(7)"
            )
            result = subprocess.run([sys.executable, "-c", script, str(path)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr.decode())
            with path.open("a+b") as stream:
                self.assertTrue(ipc._try_lock(stream))

    def test_another_process_cannot_claim_a_live_owners_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.lock"
            path.write_bytes(b"0")
            script = (
                "import sys; from figureviewer.ipc import _try_lock; "
                "stream=open(sys.argv[1],'a+b'); sys.exit(1 if _try_lock(stream) else 0)"
            )
            with path.open("a+b") as stream:
                self.assertTrue(ipc._try_lock(stream))
                result = subprocess.run([sys.executable, "-c", script, str(path)],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
