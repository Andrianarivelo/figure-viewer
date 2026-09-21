"""Combine PDFs: ordering, the merge itself, the shell funnel and the dialog.

Headless: Explorer-style natural sort, destination naming, the merge (page
order, nested bookmarks, atomic write, a source as destination, locked and
missing sources), the ``--combine`` flag through the IPC client and server,
and the app's settle timer that folds one-process-per-file launches into a
single request. Live: the real dialog (rows, move, drag, sort, remove,
validation) and the window's combine flow writing a file and opening it.
"""
from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from figureviewer import app as app_module
from figureviewer import combine as C
from figureviewer import docs as D
from figureviewer import ipc

ROOT = Path(__file__).resolve().parent.parent


def _make_pdf(path: Path, pages: int, *, toc=None, password: str = "") -> None:
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        doc.new_page(width=300, height=200).insert_text(
            (30, 60), f"{path.stem} page {index + 1}", fontsize=14)
    if toc:
        doc.set_toc(toc)
    if password:
        doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 user_pw=password, owner_pw=password)
    else:
        doc.save(str(path))
    doc.close()


class OrderingTests(unittest.TestCase):
    def test_natural_order_matches_explorer(self) -> None:
        names = ["chapter10.pdf", "Chapter2.pdf", "chapter1.pdf", "appendix.pdf",
                 "b.pdf", "a10b2.pdf", "a10b10.pdf", "A.pdf"]
        ordered = [p.name for p in C.sort_alphabetical([Path(n) for n in names])]
        self.assertEqual(ordered, ["A.pdf", "a10b2.pdf", "a10b10.pdf", "appendix.pdf",
                                   "b.pdf", "chapter1.pdf", "Chapter2.pdf",
                                   "chapter10.pdf"])

    def test_duplicates_collapse_case_insensitively(self) -> None:
        paths = [Path("C:/x/B.pdf"), Path("C:/x/a.pdf"), Path("C:/x/b.PDF"),
                 Path("C:/x/A.pdf")]
        self.assertEqual([p.name for p in C.sort_alphabetical(paths)],
                         ["a.pdf", "B.pdf"])

    def test_default_target_is_unique_and_beside_the_first_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "report.pdf", root / "annex.pdf"
            self.assertEqual(C.default_target([first, second]),
                             root / "report + 1 more.pdf")
            (root / "report + 1 more.pdf").touch()
            self.assertEqual(C.default_target([first, second]),
                             root / "report + 1 more (2).pdf")
            self.assertEqual(C.default_target([first]), root / "report combined.pdf")


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class MergeTests(unittest.TestCase):
    def test_pages_follow_the_given_order_with_nested_bookmarks(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b, c = root / "a.pdf", root / "b.pdf", root / "c.pdf"
            _make_pdf(a, 2, toc=[[1, "A intro", 1], [2, "A detail", 2]])
            _make_pdf(b, 1)
            _make_pdf(c, 3, toc=[[1, "C only", 3]])
            target = root / "out" / "joined.pdf"
            steps: list[str] = []
            pages = C.combine_pdfs([c, a, b], target,
                                   progress=lambda d, t, text: steps.append(text))
            self.assertEqual(pages, 6)
            self.assertTrue(target.exists())
            self.assertFalse(target.with_name("joined.pdf.part").exists())
            self.assertEqual(steps[-1], "Done")
            doc = pymupdf.open(str(target))
            try:
                texts = [doc[i].get_text().strip() for i in range(6)]
                self.assertEqual(texts, ["c page 1", "c page 2", "c page 3",
                                         "a page 1", "a page 2", "b page 1"])
                self.assertEqual(doc.get_toc(), [
                    [1, "c", 1], [2, "C only", 3],
                    [1, "a", 4], [2, "A intro", 4], [3, "A detail", 5],
                    [1, "b", 6]])
            finally:
                doc.close()
            plain = root / "plain.pdf"
            C.combine_pdfs([a, b], plain, bookmarks=False)
            doc = pymupdf.open(str(plain))
            try:
                self.assertEqual(doc.get_toc(), [])
            finally:
                doc.close()

    def test_a_source_may_be_the_destination(self) -> None:
        import pymupdf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = root / "a.pdf", root / "b.pdf"
            _make_pdf(a, 1)
            _make_pdf(b, 2)
            self.assertEqual(C.combine_pdfs([a, b], a), 3)
            doc = pymupdf.open(str(a))
            try:
                self.assertEqual(doc.page_count, 3)
            finally:
                doc.close()

    def test_locked_missing_and_too_few_sources_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, locked = root / "a.pdf", root / "locked.pdf"
            _make_pdf(a, 1)
            _make_pdf(locked, 1, password="secret")
            target = root / "out.pdf"
            with self.assertRaises(C.CombineError):
                C.combine_pdfs([a], target)
            with self.assertRaises(C.CombineError) as caught:
                C.combine_pdfs([a, locked], target)
            self.assertIn("locked.pdf", str(caught.exception))
            with self.assertRaises(C.CombineError):
                C.combine_pdfs([a, root / "missing.pdf"], target)
            self.assertFalse(target.exists())
            self.assertFalse(list(root.glob("*.part")))
            info = C.inspect(locked)
            self.assertTrue(info.encrypted)
            self.assertFalse(info.ok)
            self.assertEqual(C.inspect(a).pages, 1)
            self.assertIn("Not a PDF", C.inspect(root / "x.png").error)


class ShellFunnelTests(unittest.TestCase):
    def test_launcher_forwards_the_combine_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / "instance.json"
            instance.write_text('{"port": 1, "token": "t"}')
            with (mock.patch.object(ipc, "INSTANCE_PATH", instance),
                  mock.patch.object(ipc, "single_instance_enabled", return_value=True),
                  mock.patch.object(ipc, "hand_over_to_running_instance",
                                    return_value=True) as hand):
                self.assertTrue(ipc.forward_arguments(["--combine", "C:/a.pdf"]))
                hand.assert_called_once_with([Path("C:/a.pdf")], instance_path=instance,
                                             combine=True)
                hand.reset_mock()
                ipc.forward_arguments(["C:/a.pdf"])
                hand.assert_called_once_with([Path("C:/a.pdf")], instance_path=instance,
                                             combine=False)

    def test_client_marks_combine_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / "instance.json"
            instance.write_text(json.dumps({"port": 1234, "token": "test"}))
            conn = mock.MagicMock()
            conn.__enter__.return_value = conn
            conn.recv.return_value = b'{"ok": true}'
            with mock.patch.object(ipc.socket, "create_connection", return_value=conn):
                self.assertTrue(ipc.hand_over_to_running_instance(
                    [Path("a.pdf")], instance_path=instance, combine=True))
                self.assertTrue(json.loads(conn.sendall.call_args.args[0])["combine"])
                self.assertTrue(ipc.hand_over_to_running_instance(
                    [Path("a.pdf")], instance_path=instance))
                self.assertNotIn("combine", json.loads(conn.sendall.call_args.args[0]))

    def test_server_routes_combine_and_open_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(root=mock.Mock(), open_paths=mock.Mock(),
                                  combine_request=mock.Mock())
            with (mock.patch.object(app_module, "SETTINGS_DIR", Path(directory)),
                  mock.patch.object(app_module, "INSTANCE_PATH",
                                    Path(directory) / "instance.json")):
                server = app_module.InstanceServer(app)
                try:
                    for message in ({"paths": ["C:/one.pdf"], "combine": True},
                                    {"paths": ["C:/two.png"]},
                                    {"paths": ["C:/three.pdf"], "combine": True}):
                        payload = json.dumps({"token": server.token, **message}).encode()
                        with socket.create_connection(("127.0.0.1", server.port),
                                                      timeout=3) as conn:
                            conn.sendall(payload)
                            self.assertTrue(json.loads(conn.recv(1024))["ok"])
                    server._drain()
                    app.open_paths.assert_called_once_with([Path("C:/two.png")])
                    app.combine_request.assert_called_once_with(
                        [Path("C:/one.pdf"), Path("C:/three.pdf")])
                finally:
                    server.close()

    def test_launches_settle_into_one_sorted_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["b2.pdf", "b10.pdf", "a.pdf"]
            for name in names:
                (root / name).write_bytes(b"%PDF-1.4\n")
            (root / "note.txt").write_text("x")
            app = app_module.App.__new__(app_module.App)
            app.root = mock.Mock()
            app.root.after.return_value = "job"
            app._combine_pending = []
            app._combine_job = None
            app.combine_dialog = None
            window = mock.Mock()
            app.windows = [window]
            app.focus_window = mock.Mock()
            app.combine_request([root / "b2.pdf", root / "note.txt"])
            app.combine_request([root / "b10.pdf"])
            app.combine_request([root / "a.pdf"])
            # Every arrival restarts the settle timer; nothing is shown yet.
            self.assertEqual(app.root.after_cancel.call_count, 2)
            self.assertEqual(app.root.after.call_args.args[0], C.SHELL_SETTLE_MS)
            window.combine_pdfs.assert_not_called()
            app._flush_combine()
            window.combine_pdfs.assert_called_once_with(
                [root / "b2.pdf", root / "b10.pdf", root / "a.pdf"], alphabetical=True)
            app.focus_window.assert_called_once_with(window)
            # With the dialog open, late launches join it in Explorer order.
            dialog = mock.Mock()
            dialog.winfo_exists.return_value = True
            app.combine_dialog = dialog
            app.combine_request([root / "b10.pdf"])
            app._flush_combine()
            dialog.add_paths.assert_called_once_with([root / "b10.pdf"], alphabetical=True)
            window.combine_pdfs.assert_called_once()

    def test_main_routes_combine_launches(self) -> None:
        with (mock.patch.object(app_module, "load_settings",
                                return_value={"single_instance": True}),
              mock.patch.object(app_module, "hand_over_to_running_instance",
                                return_value=True) as hand):
            self.assertEqual(app_module.main(["--combine", "C:/a.pdf", "C:/b.pdf"]), 0)
            hand.assert_called_once_with([Path("C:/a.pdf"), Path("C:/b.pdf")],
                                         combine=True)

    def test_shell_scripts_register_and_remove_the_verb(self) -> None:
        register = (ROOT / "register_file_types.ps1").read_text(encoding="utf-8")
        unregister = (ROOT / "unregister_file_types.ps1").read_text(encoding="utf-8")
        verb = r"SystemFileAssociations\.pdf\shell\FigView.Combine"
        self.assertIn(verb, register)
        self.assertIn('--combine "%1"', register)
        self.assertIn('"MultiSelectModel" -Value "Player"', register)
        self.assertIn(verb, unregister)


@unittest.skipUnless(D.HAVE_FITZ, "PyMuPDF is not installed")
class CombineLiveTests(unittest.TestCase):
    def _start(self, stack: ExitStack, root: Path, target: Path):
        settings_dir = root / "settings"
        stack.enter_context(mock.patch.object(app_module, "SETTINGS_DIR", settings_dir))
        stack.enter_context(mock.patch.object(
            app_module, "SETTINGS_PATH", settings_dir / "settings.json"))
        stack.enter_context(mock.patch.object(
            app_module, "INSTANCE_PATH", settings_dir / "instance.json"))
        stack.enter_context(mock.patch.dict(app_module.DEFAULT_SETTINGS, {
            "single_instance": False, "thumb_size": 0,
            "geometry": "1100x720+40+40",
        }))
        try:
            return app_module.App(root, target)
        except Exception as exc:
            self.skipTest(f"Tk cannot open a live window here: {exc}")

    @staticmethod
    def _wait(application, predicate, seconds: float = 8.0) -> None:
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline and not predicate():
            application.root.update()
            time.sleep(0.005)

    def test_dialog_ordering_and_window_flow(self) -> None:
        from figureviewer import dialogs as DLG

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b, c = root / "a.pdf", root / "b10.pdf", root / "b2.pdf"
            _make_pdf(a, 1)
            _make_pdf(b, 2)
            _make_pdf(c, 3)
            with ExitStack() as stack:
                application = self._start(stack, root, a)
                window = application.windows[0]
                try:
                    self._wait(application, lambda: window.document is not None)

                    # --- the dialog, driven without a modal wait.
                    dialog = DLG.CombineDialog(window, window.palette, [b, a],
                                               alphabetical=True)
                    try:
                        application.root.update()
                        self.assertEqual([p.name for p in dialog.sources],
                                         ["a.pdf", "b10.pdf"])
                        self.assertEqual(dialog.target_var.get(),
                                         str(root / "a + 1 more.pdf"))
                        # Late arrival (an Explorer launch) keeps the order sorted.
                        dialog.add_paths([c], alphabetical=True)
                        self.assertEqual([p.name for p in dialog.sources],
                                         ["a.pdf", "b2.pdf", "b10.pdf"])
                        self.assertEqual(dialog.tree.item("s2", "values")[0], "2")
                        self.assertIn("3 files  ·  6 pages", dialog.summary_var.get())
                        # Move a.pdf to the end by hand...
                        dialog.tree.selection_set("s0")
                        dialog.move(1)
                        dialog.move(1)
                        self.assertEqual([p.name for p in dialog.sources],
                                         ["b2.pdf", "b10.pdf", "a.pdf"])
                        self.assertEqual(dialog.tree.selection(), ("s2",))
                        # ...and a further sorted arrival now respects that.
                        dialog.add_paths([root / "zz.pdf"], alphabetical=True)
                        self.assertEqual(dialog.sources[-1].name, "zz.pdf")
                        self.assertEqual(dialog.sources[2].name, "a.pdf")
                        self.assertIn("unreadable", dialog.summary_var.get())
                        self.assertTrue(dialog.ok_button.instate(["disabled"]))
                        dialog.tree.selection_set("s3")
                        dialog.remove()
                        self.assertFalse(dialog.ok_button.instate(["disabled"]))
                        # Drag row 0 onto row 2.
                        dialog._drag_iid = "s0"
                        dialog._drag_move(SimpleNamespace(
                            y=dialog.tree.bbox("s2")[1] + 2))
                        self.assertEqual([p.name for p in dialog.sources],
                                         ["b10.pdf", "a.pdf", "b2.pdf"])
                        dialog.sort_alphabetical()
                        self.assertEqual([p.name for p in dialog.sources],
                                         ["a.pdf", "b2.pdf", "b10.pdf"])
                        dialog.target_var.set("joined")
                        self.assertTrue(dialog.validate())
                        result = dialog.apply()
                        self.assertEqual(result["target"], root / "joined.pdf")
                        self.assertTrue(result["bookmarks"])
                    finally:
                        dialog.close()

                    # --- the window flow: dialog stubbed, real merge, opened.
                    config = {"sources": [c, b], "target": root / "flow.pdf",
                              "bookmarks": True, "open": True}
                    stub = mock.Mock()
                    stub.wait.return_value = config
                    stub.winfo_exists.return_value = False
                    with mock.patch.object(DLG, "CombineDialog", return_value=stub):
                        window.combine_pdfs([a])
                    self._wait(application, lambda: (root / "flow.pdf").exists())
                    self._wait(application, lambda: window.current is not None
                               and window.current.path == root / "flow.pdf"
                               and window.document is not None)
                    self.assertEqual(window.document.page_count, 5)
                    self.assertIsNone(application.combine_dialog)
                    self.assertIn(root / "flow.pdf", window.explicit_paths)
                finally:
                    application.quit_all()


if __name__ == "__main__":
    unittest.main()
