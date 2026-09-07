"""Regression checks for batched shell requests and recent-file updates."""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from figureviewer import app as A


class OpeningTests(unittest.TestCase):
    """Use real temporary paths and lightweight windows to count UI work."""

    def test_file_batch_updates_one_window_once(self):
        """A hundred paths require one add and one focus, preserving order."""
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{i}.png" for i in range(100)]
            for path in paths:
                path.touch()
            window = mock.Mock()
            app = A.App.__new__(A.App)
            app.windows = [window]
            app.focus_window = mock.Mock()
            A.App.open_paths(app, paths)
            window.add_files.assert_called_once_with(paths)
            app.focus_window.assert_called_once_with(window)

    def test_folders_preserve_request_order(self):
        """A folder remains an explicit boundary between file collections."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first, last = folder / "first.png", folder / "last.pdf"
            first.touch()
            last.touch()
            window = mock.Mock()
            app = A.App.__new__(A.App)
            app.windows = [window]
            app.focus_window = mock.Mock()
            app.open_paths([first, folder, last])
            self.assertEqual(window.mock_calls, [
                mock.call.add_files([first]), mock.call.reveal_path(folder),
                mock.call.add_files([last]),
            ])

    def test_recent_batch_matches_sequential_order_with_one_refresh(self):
        """Duplicates move to the front and only fifteen paths survive."""
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{i}.png" for i in range(40)]
            paths.extend([paths[4], paths[9], paths[4]])
            app = A.App.__new__(A.App)
            app.settings = {"recent_files": []}
            app.broadcast_recent = mock.Mock()
            for path in paths:
                app.push_recent_file(path)
            expected = list(app.settings["recent_files"])
            app.settings["recent_files"] = []
            app.broadcast_recent.reset_mock()
            app.push_recent_files(paths)
            self.assertEqual(app.settings["recent_files"], expected)
            app.broadcast_recent.assert_called_once_with()

    def test_server_queues_large_fragmented_requests_without_calling_tk(self):
        """ACK means queued; a UI tick combines successive shell messages."""
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(root=mock.Mock(), open_paths=mock.Mock())
            with (mock.patch.object(A, "SETTINGS_DIR", Path(directory)),
                  mock.patch.object(A, "INSTANCE_PATH", Path(directory) / "instance.json")):
                server = A.InstanceServer(app)
                try:
                    paths = [f"C:/figures/{i:05d}-" + "x" * 100 + ".png"
                             for i in range(900)]
                    for items in (paths, ["C:/last.png"]):
                        payload = json.dumps({"token": server.token, "paths": items}).encode()
                        with socket.create_connection(("127.0.0.1", server.port), timeout=3) as conn:
                            for start in range(0, len(payload), 997):
                                conn.sendall(payload[start:start + 997])
                            self.assertTrue(json.loads(conn.recv(1024))["ok"])
                    # The server thread must not schedule any Tk calls.
                    app.root.after.assert_called_once()
                    app.open_paths.assert_not_called()
                    server._drain()
                    app.open_paths.assert_called_once_with(
                        [Path(path) for path in paths + ["C:/last.png"]])
                finally:
                    server.close()


if __name__ == "__main__":
    unittest.main()
