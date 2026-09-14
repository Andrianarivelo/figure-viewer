"""Exercise real GUI folder scans, warm launcher IPC, and font resolution."""
from pathlib import Path
import json
import os
import runpy
import subprocess
import tempfile
import sys
import time

from PIL import Image, ImageGrab
import tkinter.font as tkfont

LAUNCHER = str(Path(__file__).resolve().parents[1] / "figview_linux.py")
OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="figview-checks-"))
OUTPUT.mkdir(parents=True, exist_ok=True)
sys.argv = [LAUNCHER]

with tempfile.TemporaryDirectory(prefix='figview-folder-test-') as temporary:
    root = Path(temporary)
    os.environ['XDG_CONFIG_HOME'] = str(root / 'config')
    source = runpy.run_path(LAUNCHER)
    app_module = source['app']
    requested = root / 'Selected folder with spaces'
    nested = requested / 'Nested folder'
    sibling = root / 'Sibling'
    nested.mkdir(parents=True)
    sibling.mkdir()
    direct = requested / 'Selected figure.png'
    Image.new('RGB', (480, 300), '#dbeafe').save(direct)
    Image.new('RGB', (40, 40), 'red').save(nested / 'excluded.png')
    Image.new('RGB', (40, 40), 'green').save(sibling / 'excluded.png')
    app = app_module.App()
    window = app.windows[0]
    errors = []
    app.root.report_callback_exception = lambda *args: errors.append(str(args))

    def wait(predicate):
        """Pump GUI events until actual asynchronous loading satisfies a check."""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            app.root.update()
            if predicate():
                assert not errors, errors
                return
            time.sleep(.02)
        raise AssertionError('GUI operation timed out')

    def matches():
        """Require exact folder identity and exclusion of nested/sibling files."""
        return (window.folder == requested and window._rescan_pending == 0
                and [entry.path for entry in window.entries] == [direct])

    try:
        window.scan_depth.set(-1)
        app.open_paths([requested])
        wait(matches)
        assert window.folder_var.get() == str(requested)
        assert window.scan_depth.get() == 0
        font = tkfont.nametofont('FVui', root=app.root).actual()
        assert font['family'] == 'Noto Sans', font
        assert font['size'] == 10, font
        # Start from an unrelated folder with recursion enabled, then exercise
        # the actual second-process launcher and authenticated socket handoff.
        window.set_folder(sibling)
        window.scan_depth.set(-1)
        process = subprocess.Popen([sys.executable, LAUNCHER, str(requested)])
        wait(lambda: process.poll() is not None and matches())
        assert process.returncode == 0
        wait(lambda: window.document is not None)
        window.geometry('1200x780+0+0')
        app.root.update()
        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(OUTPUT / 'figview-linux-font-preview.png')
        report = {'ok': True, 'font': font, 'cold_folder_exact': True,
                  'warm_launcher_ipc_exact': True, 'scan_depth': window.scan_depth.get(),
                  'nested_and_sibling_files_excluded': True, 'tk_callback_errors': errors}
        (OUTPUT / 'font-folder-checks.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        app.quit_all()
