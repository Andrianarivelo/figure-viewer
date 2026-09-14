#!/usr/bin/env python3
"""Install per-user FigView launchers using the current Python environment."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

APP_DIR = Path(__file__).resolve().parent


def desktop_quote(value):
    """Quote desktop-entry arguments, including reserved percent placeholders."""
    value = str(value).replace('%', '%%')
    for character in ('\\', '"', '`', '$'):
        value = value.replace(character, '\\' + character)
    return '"' + value + '"'


def main():
    """Register the app and optional Nautilus menu without system-level writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--default-pdf', action='store_true', help='Make FigView the PDF default')
    parser.add_argument('--nautilus', action='store_true', help='Install the folder context menu')
    args = parser.parse_args()
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    integration = data / 'figview'
    integration.mkdir(parents=True, exist_ok=True)
    launcher = integration / 'figview'
    # Capture the chosen interpreter so desktop launches never depend on conda activation.
    launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' '
                        + shlex.quote(str(APP_DIR / 'figview_linux.py')) + ' "$@"\n')
    launcher.chmod(0o755)
    applications = data / 'applications'
    applications.mkdir(parents=True, exist_ok=True)
    desktop = applications / 'figview.desktop'
    desktop.write_text(
        '[Desktop Entry]\nVersion=1.0\nType=Application\nName=FigView\n'
        'Comment=View figures and read or annotate PDF documents\n'
        f'Exec={desktop_quote(launcher)} %F\n'
        f'Icon={APP_DIR / "assets/figure-viewer-mark-256.png"}\n'
        'Terminal=false\nCategories=Graphics;Viewer;\n'
        'MimeType=application/pdf;\nStartupNotify=false\n')
    if args.nautilus:
        extension_dir = data / 'nautilus-python/extensions'
        extension_dir.mkdir(parents=True, exist_ok=True)
        template = (APP_DIR / 'linux/nautilus_figview.py.in').read_text()
        (extension_dir / 'figview.py').write_text(
            template.replace('__FIGVIEW_LAUNCHER__', repr(str(launcher))))
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(applications)], check=True)
    if args.default_pdf:
        subprocess.run(['xdg-mime', 'default', 'figview.desktop', 'application/pdf'], check=True)
    print(f'Installed {desktop}')
    if args.nautilus:
        print('Restart Nautilus to load the folder menu; finish any file transfers first.')


if __name__ == '__main__':
    main()
