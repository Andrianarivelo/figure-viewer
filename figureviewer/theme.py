"""Visual identity for Figure Viewer.

Everything colour- or font-related lives here so the rest of the app never
hard-codes a hex value. Two palettes are provided (dark and light); both are
applied through a single :func:`apply_theme` call that restyles every ttk
widget class the app uses.

Windows note: the *menu bar* of a ``tk.Tk`` window is drawn by the OS and
ignores colours, which is why this module also provides :class:`MenuBar`, a
fully themed menu bar built from ``ttk.Menubutton`` widgets. Only the drop-down
popups are native ``tk.Menu`` widgets, and those *do* honour colour options.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Optional


@dataclass(frozen=True)
class Palette:
    """A complete colour scheme.

    Attributes are grouped by role rather than by hue so that swapping the
    palette never requires touching widget code.
    """

    name: str
    bg: str            # window background
    panel: str         # sidebar / toolbar surface
    panel_alt: str     # slightly raised surface (entries, list background)
    border: str        # hairline separators
    text: str          # primary text
    muted: str         # secondary text
    accent: str        # brand / active colour
    accent_text: str   # text drawn on top of the accent colour
    hover: str         # hover fill for buttons and rows
    select: str        # selected row fill
    canvas: str        # image canvas backdrop
    canvas_alt: str    # second checkerboard tone
    shadow: str        # drop shadow tone behind the page
    ok: str            # success feedback
    warn: str          # warning feedback
    danger: str        # error feedback


DARK = Palette(
    name="dark",
    bg="#0f1116",
    panel="#161922",
    panel_alt="#1d212b",
    border="#2a2f3c",
    text="#e8ecf4",
    muted="#96a0b3",
    accent="#5b8def",
    accent_text="#ffffff",
    hover="#232937",
    select="#26314a",
    canvas="#0a0c10",
    canvas_alt="#12151b",
    shadow="#05070a",
    ok="#4ade80",
    warn="#fbbf24",
    danger="#f87171",
)

LIGHT = Palette(
    name="light",
    bg="#eef1f6",
    panel="#ffffff",
    panel_alt="#f4f6fa",
    border="#d8dee9",
    text="#151a22",
    muted="#5d6675",
    accent="#2f6fed",
    accent_text="#ffffff",
    hover="#e6ecf7",
    select="#d7e4ff",
    canvas="#dfe4ec",
    canvas_alt="#e9edf3",
    shadow="#b7bfcc",
    ok="#15803d",
    warn="#b45309",
    danger="#b91c1c",
)

PALETTES = {"dark": DARK, "light": LIGHT}

#: File-list width limits, in pixels. These size the whole sidebar frame -
#: padding and scrollbar included - not just the name column, so the default is
#: the width the panel used to request for itself. The minimum still fits a
#: short name plus a thumbnail; the maximum stops a stray drag from swallowing
#: the canvas. Defined here because both app.py (for the stored default) and
#: viewer.py (for the drag limits) need them, and keeping two copies is exactly
#: how they drifted apart the first time.
SIDEBAR_MIN = 190
SIDEBAR_MAX = 900
SIDEBAR_DEFAULT = 296

#: Treeview style names for the file list. ``LIST_FLAT`` has no expander
#: element; ``LIST_TREE`` keeps it, for a grouped multi-level listing.
LIST_FLAT = "Flat.Treeview"
LIST_TREE = "Tree.Treeview"


def enable_dpi_awareness() -> float:
    """Make the process DPI aware and return the screen scale factor.

    Without this call Windows bitmap-stretches the whole GUI on high-DPI
    displays, which looks blurry. Returns 1.0 on failure so callers can use the
    value unconditionally.
    """
    try:
        import ctypes

        # 1 == PROCESS_SYSTEM_DPI_AWARE. Per-monitor awareness is deliberately
        # avoided: Tk 8.6 does not re-scale fonts when a window is dragged to a
        # monitor with a different DPI, which looks worse than a slight blur.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        dpi = ctypes.windll.user32.GetDpiForSystem()
        return max(dpi / 96.0, 1.0)
    except Exception:
        return 1.0


def configure_fonts(root: tk.Misc, scale: float = 1.0) -> dict[str, tkfont.Font]:
    """Install the app's font set and return the named fonts.

    A single UI font (Segoe UI) is used at three sizes plus a monospace font
    for numeric read-outs, which keeps the interface visually calm.

    Sizes are in *points*, so Tk's ``tk scaling`` (set from the screen DPI in
    :class:`~figureviewer.app.App`) already converts them to the right number
    of pixels. ``scale`` is therefore deliberately not multiplied in here -
    doing so would apply the DPI factor twice.
    """
    base = 9
    fonts = {
        "ui": tkfont.Font(root=root, name="FVui", family="Segoe UI", size=base, exists=False),
        "ui_bold": tkfont.Font(
            root=root, name="FVuiBold", family="Segoe UI Semibold", size=base, exists=False
        ),
        "small": tkfont.Font(root=root, name="FVsmall", family="Segoe UI", size=base - 1, exists=False),
        "title": tkfont.Font(
            root=root, name="FVtitle", family="Segoe UI Semibold", size=base + 3, exists=False
        ),
        "mono": tkfont.Font(
            root=root, name="FVmono", family="Consolas", size=base - 1, exists=False
        ),
    }
    # Make plain tk widgets (Menu, Listbox, ...) pick up the same face.
    for name in ("TkDefaultFont", "TkMenuFont", "TkTextFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family="Segoe UI", size=base)
        except Exception:
            pass
    return fonts


#: Row spacing presets for the file list: (extra pixels around a thumbnail,
#: font, row height when thumbnails are off).
DENSITIES: dict[str, tuple[int, str, int]] = {
    "compact": (2, "FVsmall", 17),
    "normal": (6, "FVui", 22),
    "comfortable": (12, "FVui", 28),
}


def configure_list_density(root: tk.Misc, thumb_size: int, density: str = "compact") -> None:
    """Size the file-list rows from the thumbnail size and the density preset.

    ``rowheight`` is a style option rather than a widget option in ttk, so this
    has to go through :class:`ttk.Style` and applies to every Treeview in the
    interpreter - which is what we want, since all of them are file lists.
    """
    padding, font, bare = DENSITIES.get(density, DENSITIES["compact"])
    height = (thumb_size + padding) if thumb_size else bare
    style = ttk.Style(root)
    style.configure("Treeview", rowheight=max(15, height), font=font)
    style.configure("Treeview.Item", padding=(0, 0))
    # ttk resolves style options up the dotted name, so LIST_FLAT / LIST_TREE
    # would inherit these anyway - except rowheight, which the treeview reads
    # from its own style name only. Set it on all three.
    for name in (LIST_FLAT, LIST_TREE):
        style.configure(name, rowheight=max(15, height), font=font)


def apply_theme(root: tk.Misc, palette: Palette) -> None:
    """Restyle every ttk widget class used by the app.

    ``clam`` is used as the base theme because it is the only built-in theme
    whose element layouts fully respect background colours on Windows.
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    p = palette
    style.configure(".", background=p.bg, foreground=p.text, font="FVui",
                    borderwidth=0, focuscolor=p.accent)

    # --- containers -------------------------------------------------------
    style.configure("TFrame", background=p.bg)
    style.configure("Panel.TFrame", background=p.panel)
    style.configure("Card.TFrame", background=p.panel_alt)
    style.configure("Toolbar.TFrame", background=p.panel)
    style.configure("Status.TFrame", background=p.panel)
    style.configure("Rule.TFrame", background=p.border)
    # The drag handle between the file list and the canvas. Invisible at rest -
    # a 5 px coloured bar would be heavier than the hairline it sits next to -
    # and lit on hover, with the resize cursor doing the real signalling.
    style.configure("Sash.TFrame", background=p.bg)
    style.configure("SashActive.TFrame", background=p.accent)

    # --- labels -----------------------------------------------------------
    style.configure("TLabel", background=p.bg, foreground=p.text)
    style.configure("Panel.TLabel", background=p.panel, foreground=p.text)
    style.configure("Muted.TLabel", background=p.panel, foreground=p.muted, font="FVsmall")
    style.configure("Section.TLabel", background=p.panel, foreground=p.muted, font="FVuiBold")
    style.configure("Status.TLabel", background=p.panel, foreground=p.muted, font="FVsmall")
    style.configure("Mono.TLabel", background=p.panel, foreground=p.muted, font="FVmono")
    style.configure("Accent.TLabel", background=p.panel, foreground=p.accent, font="FVuiBold")

    # --- buttons ----------------------------------------------------------
    # width=0 is not cosmetic pedantry. ttk gives Button and Menubutton a
    # default `-width` of -11, i.e. "at least eleven characters wide", so a
    # button reading "Fit" reserved the same 139 px as one reading "Folder…"
    # and every menu title sat 150 px from the next. Natural widths make the
    # toolbar read as groups instead of a scattering, and hand the ~400 px they
    # were wasting to the folder-path box, which is the one field in this
    # window that genuinely wants the room.
    style.configure("TButton", background=p.panel_alt, foreground=p.text,
                    padding=(10, 5), relief="flat", anchor="center", width=0)
    style.map("TButton",
              background=[("pressed", p.accent), ("active", p.hover), ("disabled", p.panel)],
              foreground=[("pressed", p.accent_text), ("disabled", p.muted)])

    style.configure("Tool.TButton", background=p.panel, foreground=p.text,
                    padding=(9, 5), relief="flat", width=0)
    style.map("Tool.TButton",
              background=[("pressed", p.accent), ("active", p.hover), ("disabled", p.panel)],
              foreground=[("pressed", p.accent_text), ("disabled", p.muted)])

    style.configure("Accent.TButton", background=p.accent, foreground=p.accent_text,
                    padding=(10, 5), relief="flat", width=0)
    style.map("Accent.TButton", background=[("active", p.accent), ("pressed", p.accent)])

    # Segmented control: radio buttons that look like a joined button group.
    # The indicator element is dropped from the layout entirely - setting
    # `indicatorsize=0` still leaves a gap in the clam theme.
    for name in ("Segment.TRadiobutton", "Toggle.TCheckbutton", "Modal.TCheckbutton"):
        element = "Radiobutton" if "Radio" in name else "Checkbutton"
        style.layout(name, [
            (f"{element}.padding", {"sticky": "nswe", "children": [
                (f"{element}.label", {"sticky": "nswe"}),
            ]}),
        ])
    style.configure("Segment.TRadiobutton", background=p.panel_alt, foreground=p.muted,
                    padding=(10, 4), relief="flat", anchor="center")
    style.map("Segment.TRadiobutton",
              background=[("selected", p.accent), ("active", p.hover)],
              foreground=[("selected", p.accent_text), ("active", p.text)])

    # Two kinds of toggle, deliberately not styled alike.
    #
    # Toggle: a persistent preference such as "Auto reload". It is on almost
    # all the time, so painting it in full accent put a permanent shout in the
    # toolbar. It now reads as quietly lit rather than highlighted.
    style.configure("Toggle.TCheckbutton", background=p.panel, foreground=p.muted,
                    padding=(8, 4), relief="flat", anchor="center")
    style.map("Toggle.TCheckbutton",
              background=[("selected", p.select), ("active", p.hover)],
              foreground=[("selected", p.accent), ("active", p.text)])

    # Modal: a state that changes what the mouse does, such as Snip. This one
    # must be impossible to miss, because forgetting you are in it is the whole
    # failure mode.
    style.configure("Modal.TCheckbutton", background=p.panel, foreground=p.muted,
                    padding=(8, 4), relief="flat", anchor="center")
    style.map("Modal.TCheckbutton",
              background=[("selected", p.accent), ("active", p.hover)],
              foreground=[("selected", p.accent_text), ("active", p.text)])

    style.configure("TCheckbutton", background=p.panel, foreground=p.text)
    style.map("TCheckbutton",
              background=[("active", p.panel)],
              indicatorcolor=[("selected", p.accent), ("!selected", p.panel_alt)])

    # --- text inputs ------------------------------------------------------
    style.configure("TEntry", fieldbackground=p.panel_alt, background=p.panel_alt,
                    foreground=p.text, insertcolor=p.text, bordercolor=p.border,
                    lightcolor=p.border, darkcolor=p.border, padding=(8, 5), relief="flat")
    style.map("TEntry",
              fieldbackground=[("focus", p.panel_alt)],
              bordercolor=[("focus", p.accent)],
              lightcolor=[("focus", p.accent)],
              darkcolor=[("focus", p.accent)])

    style.configure("TCombobox", fieldbackground=p.panel_alt, background=p.panel_alt,
                    foreground=p.text, arrowcolor=p.muted, bordercolor=p.border,
                    lightcolor=p.border, darkcolor=p.border, padding=(6, 4))
    style.map("TCombobox",
              fieldbackground=[("readonly", p.panel_alt)],
              bordercolor=[("focus", p.accent)])
    root.option_add("*TCombobox*Listbox.background", p.panel_alt)
    root.option_add("*TCombobox*Listbox.foreground", p.text)
    root.option_add("*TCombobox*Listbox.selectBackground", p.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", p.accent_text)

    # --- lists ------------------------------------------------------------
    style.configure("Treeview", background=p.panel_alt, fieldbackground=p.panel_alt,
                    foreground=p.text, borderwidth=0, relief="flat")
    style.map("Treeview",
              background=[("selected", p.accent)],
              foreground=[("selected", p.accent_text)])
    style.configure("Treeview.Heading", background=p.panel, foreground=p.muted,
                    relief="flat", font="FVsmall", padding=(6, 4))
    style.map("Treeview.Heading", background=[("active", p.hover)])
    # The sidebar is a flat list *or* a folder tree, depending on the scan
    # depth, and the two want different item layouts. ttk resolves an item
    # layout per style name ("<Style>.Item"), so both can coexist and the
    # widget just switches its `style` option - see ViewerWindow._apply_filter.
    #
    # Flat: no expander element at all, so nothing reserves an empty gutter to
    # the left of every file name.
    # Tree: the expander restored, because a folder you cannot collapse is not
    # a tree, it is an indented list.
    flat_item = [
        ("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""}),
        ]}),
    ]
    tree_item = [
        ("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.indicator", {"side": "left", "sticky": ""}),
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""}),
        ]}),
    ]
    style.layout("Treeview.Item", flat_item)
    for name, item_layout in ((LIST_FLAT, flat_item), (LIST_TREE, tree_item)):
        style.layout(name, style.layout("Treeview"))
        style.layout(f"{name}.Item", item_layout)
    # A sidebar this narrow cannot afford ttk's default 20 px of indent per
    # level once a scan is three folders deep.
    try:
        style.configure(LIST_TREE, indent=14)
    except tk.TclError:
        pass

    # --- scrollbars -------------------------------------------------------
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=p.panel_alt, troughcolor=p.panel,
                        bordercolor=p.panel, arrowcolor=p.muted, relief="flat",
                        darkcolor=p.panel_alt, lightcolor=p.panel_alt, arrowsize=12)
        style.map(f"{orient}.TScrollbar",
                  background=[("active", p.muted), ("pressed", p.accent)])

    # --- misc -------------------------------------------------------------
    style.configure("TSeparator", background=p.border)
    style.configure("TPanedwindow", background=p.bg)
    style.configure("Sash", sashthickness=6, gripcount=0, background=p.border)
    style.configure("TProgressbar", background=p.accent, troughcolor=p.panel_alt,
                    bordercolor=p.panel_alt, lightcolor=p.accent, darkcolor=p.accent)
    # width=0: see the note on TButton. Without it every menu title reserved
    # eleven characters, so "Go" sat 150 px from "Tools" and the bar read as
    # six scattered words rather than a menu.
    style.configure("Menubar.TMenubutton", background=p.panel, foreground=p.text,
                    padding=(11, 5), relief="flat", arrowsize=0, width=0)
    style.map("Menubar.TMenubutton",
              background=[("active", p.hover), ("pressed", p.hover)],
              foreground=[("active", p.text)])


def style_menu(menu: tk.Menu, palette: Palette) -> None:
    """Apply palette colours to a Tk drop-down menu."""
    p = palette
    menu.configure(
        background=p.panel_alt,
        foreground=p.text,
        activebackground=p.accent,
        activeforeground=p.accent_text,
        activeborderwidth=0,
        borderwidth=1,
        relief="flat",
        selectcolor=p.accent,
        disabledforeground=p.muted,
        tearoff=0,
        font="FVui",
    )


class MenuBar(ttk.Frame):
    """A themed horizontal menu bar.

    Each top-level entry is a ``ttk.Menubutton`` with an attached ``tk.Menu``.
    Use :meth:`add_menu` to create an entry; the returned ``tk.Menu`` is styled
    and ready for ``add_command`` / ``add_checkbutton`` calls.
    """

    def __init__(self, master: tk.Misc, palette: Palette, **kwargs) -> None:
        super().__init__(master, style="Toolbar.TFrame", **kwargs)
        self.palette = palette
        self.menus: dict[str, tk.Menu] = {}

    def add_menu(self, label: str, underline: int = 0) -> tk.Menu:
        """Add a top-level menu and return its popup for population."""
        button = ttk.Menubutton(self, text=label, style="Menubar.TMenubutton",
                                underline=underline, direction="below")
        menu = tk.Menu(button)
        style_menu(menu, self.palette)
        button.configure(menu=menu)
        button.pack(side="left")
        self.menus[label] = menu
        return menu


class Tooltip:
    """A lightweight hover tooltip for any widget.

    Tk has no native tooltip; this creates a borderless ``Toplevel`` after a
    short delay and destroys it on leave. Kept deliberately small: no fading,
    no arrows, just a readable label that matches the palette.
    """

    def __init__(self, widget: tk.Misc, text: str, palette: Palette, delay: int = 550) -> None:
        self.widget = widget
        self.text = text
        self.palette = palette
        self.delay = delay
        self._after: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tip.configure(background=self.palette.border)
        label = tk.Label(
            tip, text=self.text, background=self.palette.panel_alt,
            foreground=self.palette.text, font="FVsmall", padx=8, pady=4,
            justify="left", borderwidth=0,
        )
        label.pack(padx=1, pady=1)
        try:
            tip.wm_attributes("-topmost", True)
        except Exception:
            pass
        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def bind_tooltip(widget: tk.Misc, text: str, palette: Palette) -> Tooltip:
    """Convenience wrapper so callers do not keep a reference themselves."""
    return Tooltip(widget, text, palette)


def toolbar_button(master: tk.Misc, text: str, command: Callable[[], None],
                   palette: Palette, tooltip: str = "", width: Optional[int] = None,
                   style: str = "Tool.TButton") -> ttk.Button:
    """Create a flat toolbar button with an optional tooltip."""
    button = ttk.Button(master, text=text, command=command, style=style, takefocus=False)
    if width:
        button.configure(width=width)
    if tooltip:
        bind_tooltip(button, tooltip, palette)
    return button
