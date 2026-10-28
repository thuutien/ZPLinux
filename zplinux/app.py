"""ZPLinux GTK3 user interface: a WYSIWYG designer for ZPL labels."""

from __future__ import annotations

import base64
import csv
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, List, Optional
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from . import (APP_ID, APP_NAME, __version__, printing, render, updater, zpl,  # noqa: E402
               zpl_import)
from .canvas import LabelCanvas  # noqa: E402
from .model import (BITMAP_FONTS, DATE_FORMATS, DPI_CHOICES, KIND_NAMES,  # noqa: E402
                    MM_PER_INCH, TIME_FORMATS, UNITS, BarcodeElement, magnification,
                    BoxElement, Code128Element, EllipseElement, Element, ImageElement, Label,
                    QRElement, TextElement, describe, dpmm, element_from_dict, to_unit, turn)
from . import datafile, symbology, zint  # noqa: E402
from .templates import TEMPLATES  # noqa: E402
from .widgets import CSS, PropertyForm, Toast, dim_label, icon_button  # noqa: E402

SETTINGS_FILE = os.path.join(GLib.get_user_config_dir(), "zplinux", "settings.json")
# ZPLinux was called Labelix; its settings (printers, recent files) are copied over once,
# from a normal install or from the Labelix Flatpak.
TEMPLATES_DIR = os.path.join(os.path.dirname(SETTINGS_FILE), "templates")
OLD_SETTINGS_FILES = (
    os.path.join(GLib.get_user_config_dir(), "labelix", "settings.json"),
    os.path.expanduser("~/.var/app/org.labelix.Labelix/config/labelix/settings.json"),
)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLATPAK = os.path.exists("/.flatpak-info")
INSTALLED = HERE.startswith("/usr/")
# How to start the bundled fake printer: an installed command, or the script in the source tree.
FAKE_PRINTER_CMD = (f"flatpak run --command=zplinux-fake-printer {APP_ID} --render" if FLATPAK
                    else "zplinux-fake-printer --render" if INSTALLED
                    else "python3 tools/fake_printer.py --render")
MAX_RECENT = 8
# Shown under every text and barcode field that can hold {variables}.
VARIABLES_HINT = ("Variables: {date}, {time} and {counter} fill in when printing. Any other "
                  "name, like {name}, is yours to fill in on the Label tab or from a spreadsheet.")
LABEL_EXTENSIONS = (".zplinux", ".labelix", ".json")   # .labelix: from before the rename
ZPL_EXTENSIONS = (".zpl", ".prn")    # ZebraDesigner "print to file" writes .prn
PRINTER_ICONS = {"network": "network-wired-symbolic", "device": "drive-harddisk-usb-symbolic",
                 "cups": "printer-symbolic", "file": "text-x-generic-symbolic"}

# (name, width_mm, height_mm)
SIZE_PRESETS = [
    ("4 × 6 in (shipping)", 101.6, 152.4),
    ("4 × 3 in", 101.6, 76.2),
    ("4 × 2 in", 101.6, 50.8),
    ("3 × 2 in", 76.2, 50.8),
    ("3 × 1 in", 76.2, 25.4),
    ("2.25 × 1.25 in", 57.15, 31.75),
    ("2 × 1 in", 50.8, 25.4),
    ("100 × 150 mm", 100, 150),
    ("100 × 50 mm", 100, 50),
    ("58 × 40 mm", 58, 40),
    ("50 × 30 mm", 50, 30),
]

KIND_ICONS = {
    "text": "zplinux-text-symbolic",
    "code128": "zplinux-barcode-symbolic",
    "qr": "zplinux-qr-symbolic",
    "barcode": "zplinux-barcode-symbolic",
    "box": "zplinux-box-symbolic",
    "ellipse": "zplinux-ellipse-symbolic",
    "image": "zplinux-image-symbolic",
}
TOOLS = [("text", "Text"), ("code128", "Barcode"), ("qr", "QR code"),
         ("box", "Box"), ("ellipse", "Ellipse"), ("image", "Image")]

SHORTCUTS_UI = """
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts">
    <property name="modal">1</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="visible">1</property>
        <property name="section-name">main</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="visible">1</property>
            <property name="title">Labels</property>
            {files}
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="visible">1</property>
            <property name="title">Editing</property>
            {editing}
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="visible">1</property>
            <property name="title">View</property>
            {view}
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""


def _shortcut_rows(rows):
    out = []
    for accel, title in rows:
        out.append('<child><object class="GtkShortcutsShortcut"><property name="visible">1</property>'
                   f'<property name="accelerator">{GLib.markup_escape_text(accel)}</property>'
                   f'<property name="title">{GLib.markup_escape_text(title)}</property>'
                   '</object></child>')
    return "".join(out)


def load_settings() -> dict:
    old = next((p for p in OLD_SETTINGS_FILES if os.path.exists(p)), None)
    if old and not os.path.exists(SETTINGS_FILE):
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            shutil.copyfile(old, SETTINGS_FILE)
        except OSError:
            pass
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


def thumbnail(label: Label, values: dict, max_w: int, max_h: int):
    """A cairo surface of the label scaled to fit max_w × max_h."""
    import cairo
    scale = min(max_w / label.width_dots, max_h / label.height_dots)
    w, h = max(1, int(label.width_dots * scale)), max(1, int(label.height_dots * scale))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    cr.scale(scale, scale)
    render.draw_label(cr, label.filled(values))
    return surf


def scroll_into_view(scrolled: Gtk.ScrolledWindow, widget: Gtk.Widget) -> None:
    """Scroll a panel just enough that widget is visible."""
    content = scrolled.get_child()
    if isinstance(content, Gtk.Viewport):
        content = content.get_child()
    pos = widget.translate_coordinates(content, 0, 0)
    if not pos:
        return
    top, bottom = pos[1] - 8, pos[1] + widget.get_allocated_height() + 8
    adj = scrolled.get_vadjustment()
    if top < adj.get_value():
        adj.set_value(max(adj.get_lower(), top))
    elif bottom > adj.get_value() + adj.get_page_size():
        adj.set_value(min(adj.get_upper() - adj.get_page_size(), bottom - adj.get_page_size()))


def check_printer(cfg) -> str:
    """One line about a printer: what it is and whether it is ready (network and USB
    printers say), or whether it can be reached."""
    if cfg.method in ("network", "device"):
        try:
            info = printing.identify(cfg)
            state = printing.status(cfg)
        except printing.PrintError:
            pass                       # it may not answer queries; fall back below
        else:
            mark = "✗" if state.blocking() else "✓"
            return f"{mark} {info.describe()}. {state.describe()}."
    try:
        return "✓ " + printing.test(cfg)
    except printing.PrintError as e:
        return "✗ " + str(e)


def barcode_type(el) -> str:
    """The Type list's id for a barcode or QR element."""
    if isinstance(el, Code128Element):
        return "code128"
    if isinstance(el, QRElement):
        return "qr"
    return el.symbology


def file_paths(data) -> List[str]:
    return [unquote(urlparse(u).path) for u in (data.get_uris() or [])
            if urlparse(u).scheme == "file"]


@dataclass(eq=False)          # each open document is its own tab, even if two look alike
class Document:
    """One open label (one tab): the label itself and everything that goes with editing it."""
    label: Label = field(default_factory=Label)
    values: dict = field(default_factory=dict)   # preview/print values for {variables}
    path: Optional[str] = None
    dirty: bool = False
    selected: Optional[Element] = None           # the main selected object
    also: List[Element] = field(default_factory=list)   # more selected objects
    undo_stack: List[str] = field(default_factory=list)
    redo_stack: List[str] = field(default_factory=list)
    undo_key: Optional[str] = None
    zoom: Optional[float] = None                 # None: fit to the window when shown
    panel: str = "element"
    source: Optional[str] = None                 # the ZPL file an unsaved import came from

    def name(self) -> str:
        if self.path:
            return os.path.basename(self.path)
        return os.path.basename(self.source) if self.source else "Untitled label"


def _doc_property(attr):
    return property(lambda self: getattr(self.doc, attr),
                    lambda self, v: setattr(self.doc, attr, v))


class Window(Gtk.ApplicationWindow):
    # The editor works on the document in the current tab.
    label = _doc_property("label")
    values = _doc_property("values")
    path = _doc_property("path")
    dirty = _doc_property("dirty")
    selected = _doc_property("selected")
    also = _doc_property("also")
    undo_stack = _doc_property("undo_stack")
    redo_stack = _doc_property("redo_stack")
    undo_key = _doc_property("undo_key")

    def __init__(self, app, path: Optional[str] = None):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1320, 860)
        self.set_icon_name("zplinux")
        self.settings = load_settings()
        self.printers, self.default_printer = printing.load_printers(self.settings)
        self.printer = self.printers[self.default_printer]   # where Print sends jobs
        self.unit = self.settings.get("units", "mm")
        self.doc = Document()            # stand-in while no label is open
        self.docs: List[Document] = []   # in tab order
        self._switching_tabs = False
        self.props_form: Optional[PropertyForm] = None
        self.latest: Optional[updater.Release] = None
        self.updating = False
        self._cursor = None
        self._shown_vars = None
        self._syncing_layers = False
        self._layers_sig = None
        self._pending = set()

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build_actions(app)
        self._build_header()

        overlay = Gtk.Overlay()
        self.add(overlay)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        overlay.add(root)
        self.toast = Toast()
        overlay.add_overlay(self.toast)
        self._build_update_bar(root)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        root.pack_start(self.stack, True, True, 0)
        self.stack.add_named(self._build_welcome(), "welcome")
        self.stack.add_named(self._build_editor(), "editor")

        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", lambda _w, _c, _x, _y, data, _i, _t:
                     self.drop_files(file_paths(data)))
        self.connect("key-press-event", self.on_key)
        self.connect("delete-event", self.on_delete_event)

        self.show_all()
        self.toast.set_reveal_child(False)
        self.tabs.hide()
        if path:
            self.open_path(path)
        else:
            self.show_welcome()

        # A Flatpak gets its updates from Flatpak (Flathub doesn't allow self-updating).
        if not FLATPAK and self.settings.get("check_updates", True) and \
                updater.due(self.settings.get("last_update_check", 0)):
            GLib.timeout_add_seconds(3, lambda: self.check_updates(manual=False) and False)

    # ---- actions ------------------------------------------------------------

    def _build_actions(self, app):
        def add(name, cb, accels=()):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            self.add_action(a)
            if accels:
                app.set_accels_for_action(f"win.{name}", list(accels))

        def toggle(name, key, default, cb, accels=()):
            a = Gio.SimpleAction.new_stateful(
                name, None, GLib.Variant.new_boolean(self.settings.get(key, default)))

            def activate(action, _param):
                v = not action.get_state().get_boolean()
                action.set_state(GLib.Variant.new_boolean(v))
                self.settings[key] = v
                save_settings(self.settings)
                cb(v)
            a.connect("activate", activate)
            self.add_action(a)
            if accels:
                app.set_accels_for_action(f"win.{name}", list(accels))

        add("new", self.new_label, ["<Primary>n", "<Primary>t"])
        add("close-tab", lambda: self.close_document(self.doc), ["<Primary>w"])
        add("next-tab", lambda: self.cycle_tab(1), ["<Primary>Page_Down", "<Primary>Tab"])
        add("prev-tab", lambda: self.cycle_tab(-1),
            ["<Primary>Page_Up", "<Primary><Shift>Tab", "<Primary><Shift>ISO_Left_Tab"])
        add("open", self.open_dialog, ["<Primary>o"])
        add("save", self.save, ["<Primary>s"])
        add("save-as", self.save_as, ["<Primary><Shift>s"])
        add("save-template", self.save_as_template)
        add("export-zpl", self.export_zpl, ["<Primary>e"])
        add("export-png", self.export_png)
        add("print", self.print_label, ["<Primary>p"])
        add("print-csv", self.print_csv)
        add("printer-settings", self.printer_dialog)
        add("preferences", self.preferences_dialog, ["<Primary>comma"])
        add("shortcuts", self.shortcuts_window, ["<Primary>question", "<Primary>F1"])
        add("check-updates", lambda: self.check_updates(manual=True))
        add("about", self.about)
        add("undo", self.undo, ["<Primary>z"])
        add("redo", self.redo, ["<Primary><Shift>z", "<Primary>y"])
        add("duplicate", self.duplicate_selected, ["<Primary>d"])
        add("rotate", lambda: self.rotate_selected(90), ["<Primary>r"])
        add("lock", self.toggle_lock, ["<Primary>l"])
        add("zoom-in", lambda: self.set_zoom(self.canvas.zoom * 1.25),
            ["<Primary>plus", "<Primary>equal"])
        add("zoom-out", lambda: self.set_zoom(self.canvas.zoom / 1.25), ["<Primary>minus"])
        add("zoom-100", lambda: self.set_zoom(1.0), ["<Primary>0"])
        add("zoom-fit", self.zoom_fit, ["<Primary>9"])
        toggle("show-grid", "show_grid", False, self._set_grid, ["<Primary>apostrophe"])
        toggle("snap", "snap", True, self._set_snap)

    def _set_grid(self, v):
        self.canvas.show_grid = v
        self.canvas.queue_draw()

    def _set_snap(self, v):
        self.canvas.snap = v

    EDITOR_ACTIONS = ("save", "save-as", "save-template", "export-zpl", "export-png", "print", "print-csv",
                      "undo", "redo", "duplicate", "zoom-in", "zoom-out", "zoom-100", "zoom-fit",
                      "close-tab", "next-tab", "prev-tab", "rotate", "lock")

    def _editor_actions_enabled(self, on: bool):
        for name in self.EDITOR_ACTIONS:
            self.lookup_action(name).set_enabled(on)
        if on:
            self._update_history_buttons()

    def in_editor(self) -> bool:
        return self.stack.get_visible_child_name() == "editor"

    # ---- header -------------------------------------------------------------

    def _build_header(self):
        hb = Gtk.HeaderBar(show_close_button=True, title=APP_NAME)
        hb.get_style_context().add_class("accent-bar")
        self.set_titlebar(hb)
        self.header = hb

        def action_button(icon, tip, action):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.set_action_name(action)
            return b

        self.back_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.back_btn.set_tooltip_text("Back to the open labels (Esc)")
        self.back_btn.connect("clicked", lambda *_: self.show_editor())
        self.back_btn.set_no_show_all(True)
        hb.pack_start(self.back_btn)
        hb.pack_start(action_button("document-new-symbolic", "New label (Ctrl+N)", "win.new"))
        hb.pack_start(action_button("document-open-symbolic", "Open (Ctrl+O)", "win.open"))
        hist = Gtk.Box()
        hist.get_style_context().add_class("linked")
        hist.pack_start(action_button("edit-undo-symbolic", "Undo (Ctrl+Z)", "win.undo"),
                        False, False, 0)
        hist.pack_start(action_button("edit-redo-symbolic", "Redo (Ctrl+Shift+Z)", "win.redo"),
                        False, False, 0)
        hb.pack_start(hist)
        self.hist_box = hist
        # Grid and snapping, at hand while designing (the same toggles as Ctrl+' etc.)
        view = Gtk.Box()
        view.get_style_context().add_class("linked")
        for icon, tip, action in (
                ("zplinux-grid-symbolic", "Show grid (Ctrl+')", "win.show-grid"),
                ("zplinux-snap-symbolic", "Snap to edges, centres and other objects "
                                          "(hold Alt to place freely)", "win.snap")):
            b = Gtk.ToggleButton()
            b.add(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            b.set_tooltip_text(tip)
            b.set_action_name(action)
            view.pack_start(b, False, False, 0)
        hb.pack_start(view)
        self.view_box = view

        menu = Gio.Menu()
        for items in ((("Save As…", "win.save-as"), ("Save as Template…", "win.save-template"),
                       ("Export ZPL…", "win.export-zpl"),
                       ("Export PNG Preview…", "win.export-png")),
                      (("Print a Batch…", "win.print-csv"),
                       ("Printers…", "win.printer-settings")),
                      (("Preferences", "win.preferences"),
                       ("Keyboard Shortcuts", "win.shortcuts"),
                       ("Check for Updates", "win.check-updates"),
                       (f"About {APP_NAME}", "win.about"))):
            sec = Gio.Menu()
            for text, action in items:
                if FLATPAK and action == "win.check-updates":
                    continue
                sec.append(text, action)
            menu.append_section(None, sec)
        mb = Gtk.MenuButton(menu_model=menu, use_popover=True)
        mb.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        mb.set_tooltip_text("Main menu")
        hb.pack_end(mb)

        # Print, with a popover for choosing the printer
        pbox = Gtk.Box()
        pbox.get_style_context().add_class("linked")
        pb = Gtk.Button()
        inner = Gtk.Box(spacing=6)
        inner.pack_start(Gtk.Image.new_from_icon_name("printer-symbolic", Gtk.IconSize.BUTTON),
                         False, False, 0)
        inner.pack_start(Gtk.Label(label="Print"), False, False, 0)
        pb.add(inner)
        pb.get_style_context().add_class("suggested-action")
        pb.set_tooltip_text("Print this label (Ctrl+P)")
        pb.set_action_name("win.print")
        pbox.pack_start(pb, False, False, 0)
        pm = Gtk.MenuButton()
        pm.get_style_context().add_class("suggested-action")
        pm.add(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON))
        pm.set_tooltip_text("Choose printer")
        pm.set_popover(self._build_print_popover())
        pbox.pack_start(pm, False, False, 0)
        hb.pack_end(pbox)
        self.print_box = pbox

        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.set_tooltip_text("Save (Ctrl+S)")
        self.save_btn.set_action_name("win.save")
        hb.pack_end(self.save_btn)

    def _build_print_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        box.set_size_request(300, -1)
        pop.add(box)
        t = Gtk.Label(label="Print to", xalign=0)
        t.get_style_context().add_class("card-title")
        box.pack_start(t, False, False, 0)
        self.pp_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.pp_list.get_style_context().add_class("card")
        self.pp_list.connect("row-activated", self._on_printer_row)
        box.pack_start(self.pp_list, False, False, 0)
        self.pp_result = dim_label("")
        self.pp_result.set_selectable(True)
        self.pp_result.set_no_show_all(True)
        box.pack_start(self.pp_result, False, False, 0)
        btns = Gtk.Box(spacing=6, homogeneous=True)
        change = Gtk.Button(label="Printers…")
        change.connect("clicked", lambda *_: (pop.popdown(), self.printer_dialog()))
        test = Gtk.Button(label="Check Printer")
        test.connect("clicked", lambda *_: self._test_printer())
        btns.pack_start(change, True, True, 0)
        btns.pack_start(test, True, True, 0)
        box.pack_start(btns, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 2)
        csvb = Gtk.ModelButton(text="Print a Batch from a File…")
        csvb.set_action_name("win.print-csv")
        box.pack_start(csvb, False, False, 0)
        calib = Gtk.ModelButton(text="Calibrate Label Stock…")
        calib.connect("clicked", lambda *_: self.calibrate_printer())
        box.pack_start(calib, False, False, 0)
        box.show_all()
        pop.connect("show", lambda *_: self._update_printer_info())
        return pop

    def _update_printer_info(self):
        for row in self.pp_list.get_children():
            self.pp_list.remove(row)
        for i, cfg in enumerate(self.printers):
            row = Gtk.ListBoxRow()
            row.printer = cfg
            hbox = Gtk.Box(spacing=10, margin=8)
            hbox.pack_start(Gtk.Image.new_from_icon_name(PRINTER_ICONS[cfg.method],
                                                         Gtk.IconSize.DND), False, False, 0)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = Gtk.Label(label=cfg.title(), xalign=0, ellipsize=Pango.EllipsizeMode.END)
            col.pack_start(name, False, False, 0)
            sub = cfg.describe() + (" · Default" if i == self.default_printer else "")
            col.pack_start(dim_label(sub, wrap=False), False, False, 0)
            hbox.pack_start(col, True, True, 0)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic", Gtk.IconSize.BUTTON)
            check.set_opacity(1 if cfg is self.printer else 0)
            hbox.pack_end(check, False, False, 0)
            row.add(hbox)
            self.pp_list.add(row)
        self.pp_list.show_all()
        self.pp_result.hide()

    def _on_printer_row(self, lb, row):
        self.printer = row.printer
        self._update_printer_info()

    def _test_printer(self):
        msg = check_printer(self.printer)
        if msg.startswith("✗") and self.printer.method == "network" and \
                self.printer.host in ("127.0.0.1", "localhost"):
            msg += f"\nNo printer yet? Run: {FAKE_PRINTER_CMD}"
        self.pp_result.set_text(msg)
        self.pp_result.show()

    def calibrate_printer(self):
        cfg = self.printer
        if cfg.method == "file":
            self.notify("Choose a printer first: this one saves to a file.")
            return
        if not self.ask(f"Calibrate {cfg.title()}?",
                        "The printer feeds a few labels to measure the label stock. Do this "
                        "after loading a different kind of label.", "Calibrate"):
            return
        try:
            printing.calibrate(cfg)
        except (printing.PrintError, OSError) as e:
            self.error("Could not calibrate", str(e))
            return
        self.notify(f"{cfg.title()} is calibrating")

    def _preflight(self, cfg) -> bool:
        """Before printing to a network or USB printer, ask it what it is and how it
        is; warn about what would spoil the print. True to go ahead. A printer that
        doesn't answer is printed to anyway."""
        if cfg.method not in ("network", "device"):
            return True
        try:
            info = printing.identify(cfg, timeout=1.5)
            state = printing.status(cfg, timeout=1.5)
        except printing.PrintError:
            return True
        name = info.model or cfg.title()
        if state.blocking():
            problems = "; ".join(state.blocking())
            if not self.ask(f"{name} can't print right now",
                            f"It reports: {problems}. Fix that and print again, or send the "
                            "job anyway; the printer prints it once it's fixed.",
                            "Send Anyway"):
                return False
        lab = self.label
        if info.dpi and info.dpi != lab.dpi:
            if not self.ask(f"The {name} prints at {info.dpi} dpi",
                            f"This label is set for {lab.dpi} dpi, so it would print "
                            f"{'larger' if info.dpi < lab.dpi else 'smaller'} than designed. "
                            f"Set Resolution on the Label tab to {info.dpi} dpi.",
                            "Print Anyway"):
                return False
        width_mm = lab.stock_mm[0]
        max_mm = info.max_width_dots / dpmm(info.dpi) if info.max_width_dots else 0
        if max_mm and width_mm > max_mm + 0.5:
            if not self.ask(f"The label is too wide for the {name}",
                            f"The label prints {width_mm:.0f} mm wide, but the {name} prints "
                            f"at most {max_mm:.0f} mm; the rest would be cut off. For a "
                            "landscape design, try Rotate when printing on the Label tab.",
                            "Print Anyway"):
                return False
        if state.paused:
            self.notify(f"{name} is paused: the label prints when you resume it")
        return True

    # ---- update bar ---------------------------------------------------------

    def _build_update_bar(self, root):
        bar = Gtk.InfoBar(message_type=Gtk.MessageType.INFO, show_close_button=True)
        area = bar.get_content_area()
        area.pack_start(Gtk.Image.new_from_icon_name("software-update-available-symbolic",
                                                     Gtk.IconSize.BUTTON), False, False, 0)
        self.update_text = Gtk.Label(xalign=0, wrap=True)
        area.pack_start(self.update_text, True, True, 0)
        self.update_notes_btn = bar.add_button("What's New", 1)
        self.update_install_btn = bar.add_button("Install", 2)
        self.update_install_btn.get_style_context().add_class("suggested-action")
        self.update_restart_btn = bar.add_button("Restart Now", 3)
        self.update_restart_btn.get_style_context().add_class("suggested-action")
        bar.connect("response", self._on_update_bar)
        bar.set_no_show_all(True)
        area.show_all()
        self.update_bar = bar
        root.pack_start(bar, False, False, 0)

    def _on_update_bar(self, bar, resp):
        if resp == Gtk.ResponseType.CLOSE:
            bar.hide()
        elif resp == 1 and self.latest:
            Gtk.show_uri_on_window(self, self.latest.url, Gdk.CURRENT_TIME)
        elif resp == 2:
            self.install_update()
        elif resp == 3:
            self.restart()

    # ---- welcome ------------------------------------------------------------

    def _build_welcome(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                        margin_top=36, margin_bottom=36, margin_start=24, margin_end=24, spacing=10)
        outer.set_size_request(940, -1)
        scroll.add(outer)

        hero = Gtk.Box(spacing=16, halign=Gtk.Align.START)
        logo = Gtk.Image.new_from_icon_name("zplinux", Gtk.IconSize.DIALOG)
        logo.set_pixel_size(64)
        hero.pack_start(logo, False, False, 0)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2)
        title = Gtk.Label(label=APP_NAME, xalign=0)
        title.get_style_context().add_class("welcome-title")
        sub = Gtk.Label(label="Design and print labels for Zebra and other ZPL printers.", xalign=0)
        sub.get_style_context().add_class("welcome-sub")
        tb.pack_start(title, False, False, 0)
        tb.pack_start(sub, False, False, 0)
        hero.pack_start(tb, False, False, 0)
        outer.pack_start(hero, False, False, 12)

        head = Gtk.Label(label="Start a new label", xalign=0)
        head.get_style_context().add_class("sidebar-title")
        outer.pack_start(head, False, False, 0)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=2, max_children_per_line=5,
                           column_spacing=12, row_spacing=12)
        for _tid, name, size, factory in TEMPLATES:
            flow.add(self._template_card(name, size, factory))
        flow.add(self._custom_card())
        outer.pack_start(flow, False, False, 0)

        self.user_head = Gtk.Label(label="Your templates", xalign=0)
        self.user_head.get_style_context().add_class("sidebar-title")
        self.user_head.set_no_show_all(True)
        outer.pack_start(self.user_head, False, False, 6)
        self.user_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                     min_children_per_line=2, max_children_per_line=5,
                                     column_spacing=12, row_spacing=12)
        self.user_flow.set_no_show_all(True)
        outer.pack_start(self.user_flow, False, False, 0)

        self.recent_head = Gtk.Label(label="Recent", xalign=0)
        self.recent_head.get_style_context().add_class("sidebar-title")
        self.recent_head.set_no_show_all(True)
        outer.pack_start(self.recent_head, False, False, 6)
        self.recent_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.recent_list.get_style_context().add_class("card")
        self.recent_list.set_no_show_all(True)
        self.recent_list.connect("row-activated", lambda _l, row: self.open_path(row.path))
        outer.pack_start(self.recent_list, False, False, 0)
        openb = Gtk.Button(label="Open a Label…", halign=Gtk.Align.START)
        openb.set_action_name("win.open")
        outer.pack_start(openb, False, False, 6)
        return scroll

    def _card(self, picture: Gtk.Widget, name: str, sub: str, cb: Callable) -> Gtk.Widget:
        btn = Gtk.Button()
        btn.get_style_context().add_class("template-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(picture, False, False, 4)
        n = Gtk.Label(label=name, xalign=0)
        n.get_style_context().add_class("template-name")
        box.pack_start(n, False, False, 0)
        box.pack_start(dim_label(sub, wrap=False), False, False, 0)
        btn.add(box)
        btn.connect("clicked", lambda *_: cb())
        return btn

    def _template_card(self, name, size, factory, menu=None) -> Gtk.Widget:
        """A start-screen card; menu: (text, callback) pairs for its right-click menu."""
        lab, values = factory()
        surf = thumbnail(lab, values, 150, 100)
        area = Gtk.DrawingArea()
        area.set_size_request(160, 108)

        def draw(w, cr):
            a = w.get_allocation()
            x = int((a.width - surf.get_width()) / 2)
            y = int((a.height - surf.get_height()) / 2)
            cr.set_source_rgba(0, 0, 0, 0.12)
            cr.rectangle(x + 1, y + 2, surf.get_width(), surf.get_height())
            cr.fill()
            cr.set_source_surface(surf, x, y)
            cr.paint()
            cr.set_source_rgba(0, 0, 0, 0.18)
            cr.set_line_width(1)
            cr.rectangle(x + 0.5, y + 0.5, surf.get_width() - 1, surf.get_height() - 1)
            cr.stroke()
        area.connect("draw", draw)
        card = self._card(area, name, size, lambda: self.start_from(factory))
        if menu:
            def on_press(_w, ev):
                if ev.button != 3:
                    return False
                m = Gtk.Menu()
                for text, cb in menu:
                    it = Gtk.MenuItem(label=text)
                    it.connect("activate", lambda *_a, cb=cb: cb())
                    m.append(it)
                m.show_all()
                m.attach_to_widget(card, None)
                m.popup_at_pointer(ev)
                return True
            card.connect("button-press-event", on_press)
        return card

    # ---- your templates -----------------------------------------------------

    def _refresh_user_templates(self):
        for c in self.user_flow.get_children():
            self.user_flow.remove(c)
        found = []
        if os.path.isdir(TEMPLATES_DIR):
            for fname in sorted(os.listdir(TEMPLATES_DIR), key=str.lower):
                if not fname.endswith(".zplinux"):
                    continue
                path = os.path.join(TEMPLATES_DIR, fname)
                try:
                    with open(path) as f:
                        text = f.read()
                    Label.from_json(text)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                found.append((fname[:-len(".zplinux")], path, text))
        for name, path, text in found:
            def factory(text=text):
                return Label.from_json(text), json.loads(text).get("preview_values", {})
            lab = Label.from_json(text)
            if self.unit == "in":
                size = f"{lab.width_mm / MM_PER_INCH:g} × {lab.height_mm / MM_PER_INCH:g} in"
            else:
                size = f"{lab.width_mm:g} × {lab.height_mm:g} mm"
            card = self._template_card(name, size, factory,
                                       menu=[("Delete Template",
                                              lambda p=path, n=name: self.delete_template(p, n))])
            card.set_tooltip_text("Right-click to delete")
            card.show_all()
            self.user_flow.add(card)
        self.user_flow.set_visible(bool(found))
        self.user_head.set_visible(bool(found))
        for child in self.user_flow.get_children():
            child.show()

    def save_as_template(self):
        """Keep the current label on the start screen, to start new labels from."""
        dlg = Gtk.Dialog(title="Save as Template", transient_for=self, modal=True,
                         use_header_bar=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class(
            "suggested-action")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
        default = os.path.splitext(self.doc.name())[0]
        entry = Gtk.Entry(text="" if default == "Untitled label" else default,
                          placeholder_text="e.g. Sorter label", activates_default=True)
        box.pack_start(dim_label("Your templates appear on the start screen. Starting from "
                                 "one makes a new label; the template stays as it is."),
                       False, False, 0)
        box.pack_start(entry, False, False, 0)
        dlg.get_content_area().add(box)
        dlg.set_default_size(400, -1)
        dlg.show_all()
        name = None
        while dlg.run() == Gtk.ResponseType.OK:
            name = re.sub(r'[\\/:*?"<>|]', "-", entry.get_text().strip())
            if not name:
                continue
            path = os.path.join(TEMPLATES_DIR, name + ".zplinux")
            if os.path.exists(path) and not self.ask(
                    f"Replace the template “{name}”?", "A template with that name exists.",
                    "Replace"):
                name = None
                continue
            break
        else:
            name = None
        dlg.destroy()
        if not name:
            return
        try:
            os.makedirs(TEMPLATES_DIR, exist_ok=True)
            with open(os.path.join(TEMPLATES_DIR, name + ".zplinux"), "w") as f:
                f.write(self._document_json())
        except OSError as e:
            self.error("Could not save the template", str(e))
            return
        self._refresh_user_templates()
        self.notify(f"Saved “{name}” as a template")

    def delete_template(self, path, name):
        if not self.ask(f"Delete the template “{name}”?",
                        "Labels made from it are not affected.", "Delete"):
            return
        try:
            os.remove(path)
        except OSError as e:
            self.error("Could not delete the template", str(e))
            return
        self._refresh_user_templates()

    def _custom_card(self) -> Gtk.Widget:
        img = Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.DIALOG)
        img.set_size_request(160, 108)
        img.get_style_context().add_class("dim")

        def custom():
            w = self.settings.get("last_width_mm", 101.6)
            h = self.settings.get("last_height_mm", 50.8)
            d = self.settings.get("last_dpi", 203)
            if self.start_from(lambda: (Label(width_mm=w, height_mm=h, dpi=d), {})):
                self.panel_stack.set_visible_child_name("label")
        return self._card(img, "Custom size", "Any width and height", custom)

    def _refresh_recent(self):
        for c in self.recent_list.get_children():
            self.recent_list.remove(c)
        recent = [p for p in self.settings.get("recent", []) if os.path.isfile(p)]
        for p in recent:
            row = Gtk.ListBoxRow()
            row.path = p
            box = Gtk.Box(spacing=12, margin=10)
            box.pack_start(Gtk.Image.new_from_icon_name("text-x-generic-symbolic",
                                                        Gtk.IconSize.BUTTON), False, False, 0)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            col.pack_start(Gtk.Label(label=os.path.basename(p), xalign=0), False, False, 0)
            folder = dim_label(os.path.dirname(p).replace(os.path.expanduser("~"), "~", 1),
                               wrap=False)
            folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            col.pack_start(folder, False, False, 0)
            box.pack_start(col, True, True, 0)
            row.add(box)
            row.show_all()
            self.recent_list.add(row)
        self.recent_list.set_visible(bool(recent))
        self.recent_head.set_visible(bool(recent))

    def _add_recent(self, path):
        path = os.path.abspath(path)
        rec = [p for p in self.settings.get("recent", []) if p != path]
        self.settings["recent"] = [path] + rec[:MAX_RECENT - 1]
        save_settings(self.settings)

    def show_welcome(self):
        self._refresh_recent()
        self._refresh_user_templates()
        self.stack.set_visible_child_name("welcome")
        for w in (self.hist_box, self.view_box, self.print_box, self.save_btn):
            w.hide()
        self.back_btn.set_visible(bool(self.docs))
        self.tabs.hide()
        self._editor_actions_enabled(False)
        self.header.set_subtitle(None)

    def show_editor(self):
        self.stack.set_visible_child_name("editor")
        for w in (self.hist_box, self.view_box, self.print_box, self.save_btn):
            w.show()
        self.back_btn.hide()
        self.tabs.show()
        self._editor_actions_enabled(True)
        self.update_title()

    # ---- editor layout ------------------------------------------------------

    def _build_editor(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # The notebook is only the tab strip: its pages are empty and every tab shares
        # the editor below, which shows the current tab's document.
        self.tabs = Gtk.Notebook(scrollable=True, show_border=False)
        self.tabs.get_style_context().add_class("doc-tabs")
        self.tabs.connect("switch-page", self._on_switch_tab)
        self.tabs.connect("page-reordered", self._on_tabs_reordered)
        new_tab = icon_button("tab-new-symbolic", "New label (Ctrl+T)", self.new_label)
        new_tab.show()
        self.tabs.set_action_widget(new_tab, Gtk.PackType.END)
        self.tabs.set_no_show_all(True)
        outer.pack_start(self.tabs, False, False, 0)
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.pack_start(main, True, True, 0)

        # Left sidebar: insert tiles + layers
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.get_style_context().add_class("sidebar")
        side.set_size_request(230, -1)
        side.set_hexpand(False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=8)
        side.pack_start(inner, True, True, 0)
        t = Gtk.Label(label="Insert", xalign=0)
        t.get_style_context().add_class("sidebar-title")
        inner.pack_start(t, False, False, 0)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        for i, (kind, text) in enumerate(TOOLS):
            b = Gtk.Button()
            b.get_style_context().add_class("tile")
            b.set_tooltip_text(f"Add {text.lower()}" + (" from a file" if kind == "image" else ""))
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            v.pack_start(Gtk.Image.new_from_icon_name(KIND_ICONS[kind], Gtk.IconSize.LARGE_TOOLBAR),
                         False, False, 0)
            v.pack_start(Gtk.Label(label=text), False, False, 0)
            b.add(v)
            b.connect("clicked", lambda _w, k=kind: self.add_element(k))
            grid.attach(b, i % 3, i // 3, 1, 1)
        inner.pack_start(grid, False, False, 0)
        t = Gtk.Label(label="Layers", xalign=0)
        t.get_style_context().add_class("sidebar-title")
        inner.pack_start(t, False, False, 6)
        self.layers = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE,
                                  activate_on_single_click=False)
        self.layers.get_style_context().add_class("layers")
        self.layers.connect("selected-rows-changed", self._on_layer_selected)
        placeholder = dim_label("Nothing on the label yet. Add something above, or drop an "
                                "image onto the label.")
        placeholder.set_margin_start(6)
        placeholder.show()
        self.layers.set_placeholder(placeholder)
        ls = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        ls.add(self.layers)
        inner.pack_start(ls, True, True, 0)
        main.pack_start(side, False, False, 0)
        main.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        # Canvas with floating controls
        over = Gtk.Overlay()
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.get_style_context().add_class("canvas-bg")
        self.canvas = LabelCanvas(self)
        self.canvas.show_grid = self.settings.get("show_grid", False)
        self.canvas.grid_mm = float(self.settings.get("grid_mm", 5))
        self.canvas.snap = self.settings.get("snap", True)
        self.canvas.connect("scroll-event", self.on_scroll)
        self.scroller.add(self.canvas)
        over.add(self.scroller)

        zbar = Gtk.Box(spacing=2, halign=Gtk.Align.END, valign=Gtk.Align.END)
        zbar.get_style_context().add_class("floating-bar")
        zbar.pack_start(icon_button("zoom-out-symbolic", "Zoom out (Ctrl+−)",
                                    lambda: self.set_zoom(self.canvas.zoom / 1.25)), False, False, 0)
        self.zoom_btn = Gtk.Button(label="100%", relief=Gtk.ReliefStyle.NONE)
        self.zoom_btn.set_tooltip_text("Actual size: one printer dot per pixel (Ctrl+0)")
        self.zoom_btn.connect("clicked", lambda *_: self.set_zoom(1.0))
        zbar.pack_start(self.zoom_btn, False, False, 0)
        zbar.pack_start(icon_button("zoom-in-symbolic", "Zoom in (Ctrl++)",
                                    lambda: self.set_zoom(self.canvas.zoom * 1.25)), False, False, 0)
        zbar.pack_start(icon_button("zoom-fit-best-symbolic", "Fit to window (Ctrl+9)",
                                    self.zoom_fit), False, False, 0)
        over.add_overlay(zbar)
        self.status = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.END)
        self.status.get_style_context().add_class("status-pill")
        over.add_overlay(self.status)
        over.set_overlay_pass_through(self.status, True)
        main.pack_start(over, True, True, 0)
        main.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        # Right panel
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right.set_size_request(350, -1)
        right.set_hexpand(False)
        self.panel_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        sw = Gtk.StackSwitcher(stack=self.panel_stack, halign=Gtk.Align.CENTER, margin=8)
        right.pack_start(sw, False, False, 0)
        right.pack_start(self.panel_stack, True, True, 0)
        self.props_box = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.label_box = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.panel_stack.add_titled(self.props_box, "element", "Element")
        self.panel_stack.add_titled(self.label_box, "label", "Label")
        self.panel_stack.add_titled(self._build_zpl_page(), "zpl", "ZPL")
        main.pack_start(right, False, False, 0)
        return outer

    def _build_zpl_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        box.pack_start(dim_label("The exact commands sent to the printer, with the current "
                                 "variable values filled in."), False, False, 0)
        self.zpl_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        self.zpl_view.get_style_context().add_class("zpl-view")
        zs = Gtk.ScrolledWindow()
        zs.add(self.zpl_view)
        card = Gtk.Box()
        card.get_style_context().add_class("card")
        card.pack_start(zs, True, True, 0)
        box.pack_start(card, True, True, 0)
        row = Gtk.Box(spacing=6, homogeneous=True)
        copy = Gtk.Button(label="Copy")
        copy.connect("clicked", lambda *_: self.copy_zpl())
        exp = Gtk.Button(label="Export…")
        exp.set_action_name("win.export-zpl")
        row.pack_start(copy, True, True, 0)
        row.pack_start(exp, True, True, 0)
        box.pack_start(row, False, False, 0)
        return box

    # ---- document -----------------------------------------------------------

    def new_label(self):
        """Show the start screen; the label picked there opens in a new tab."""
        self.show_welcome()

    def start_from(self, factory: Callable) -> bool:
        label, values = factory()
        self.add_document(Document(label=label, values=values))
        return True

    def add_document(self, doc: Document):
        page = Gtk.Box()
        page.doc = doc
        tab = Gtk.Box(spacing=4)
        tab.title = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=24)
        tab.pack_start(tab.title, True, True, 0)
        close = icon_button("window-close-symbolic", "Close (Ctrl+W)",
                            lambda: self.close_document(doc))
        close.get_style_context().add_class("tab-close")
        tab.pack_start(close, False, False, 0)
        tab.show_all()
        page.show()
        doc.page, doc.tab = page, tab
        self.docs.append(doc)
        self._update_tab(doc)
        n = self.tabs.append_page(page, tab)
        self.tabs.set_tab_reorderable(page, True)
        self.tabs.child_set_property(page, "tab-expand", False)
        self.show_editor()
        if self.tabs.get_current_page() == n:
            self.switch_to(doc)       # the first tab: switch-page already fired
        else:
            self.tabs.set_current_page(n)

    def _update_tab(self, doc: Document):
        text = ("• " if doc.dirty else "") + doc.name()
        doc.tab.title.set_text(text)
        doc.tab.title.set_width_chars(min(len(text) + 2, 26))
        doc.tab.set_tooltip_text(doc.path or doc.name())

    def _on_switch_tab(self, _nb, page, _n):
        if not self._switching_tabs:
            self.switch_to(page.doc)

    def _on_tabs_reordered(self, nb, _page, _n):
        self.docs = [nb.get_nth_page(i).doc for i in range(nb.get_n_pages())]

    def switch_to(self, doc: Document):
        """Show doc in the editor, keeping the old document's zoom and panel."""
        if self.doc in self.docs and self.doc is not doc:
            self.doc.zoom = self.canvas.zoom
            self.doc.panel = self.panel_stack.get_visible_child_name()
        self.doc = doc
        self._switching_tabs = True
        self.tabs.set_current_page(self.tabs.page_num(doc.page))
        self._switching_tabs = False
        self.props_form = None
        self._layers_sig = None
        self._shown_vars = None
        self.refresh_label_panel()
        self.build_props()
        self.panel_stack.set_visible_child_name(doc.panel)
        self.refresh_zpl()
        self.refresh_layers()
        self.update_status()
        self.show_editor()
        self._update_history_buttons()
        if doc.zoom is None:
            self.canvas.resize_to_label()
            GLib.idle_add(self.zoom_fit)
        else:
            self.set_zoom(doc.zoom)

    def cycle_tab(self, step: int):
        if len(self.docs) > 1 and self.in_editor():
            i = self.docs.index(self.doc)
            self.switch_to(self.docs[(i + step) % len(self.docs)])

    def close_document(self, doc: Document) -> bool:
        if doc not in self.docs:
            return True
        if doc.dirty:
            self.switch_to(doc)
            if not self.confirm_discard():
                return False
        self.docs.remove(doc)
        self._switching_tabs = True
        self.tabs.remove_page(self.tabs.page_num(doc.page))
        self._switching_tabs = False
        if not self.docs:
            self.doc = Document()
            self.show_welcome()
        elif doc is self.doc:
            self.doc = Document()     # closed: nothing to remember for it
            self.switch_to(self.docs[min(self.tabs.get_current_page(), len(self.docs) - 1)])
        return True

    def open_dialog(self):
        dlg = Gtk.FileChooserNative.new("Open Label", self, Gtk.FileChooserAction.OPEN, None, None)
        dlg.set_select_multiple(True)
        for name, exts in (("Labels and ZPL files", LABEL_EXTENSIONS + ZPL_EXTENSIONS),
                           ("ZPLinux labels (*.zplinux)", LABEL_EXTENSIONS),
                           ("ZPL from ZebraDesigner and others (*.prn, *.zpl)", ZPL_EXTENSIONS)):
            f = Gtk.FileFilter()
            f.set_name(name)
            for ext in exts:
                f.add_pattern("*" + ext)
                f.add_pattern("*" + ext.upper())
            dlg.add_filter(f)
        self._set_dialog_folder(dlg)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            for path in dlg.get_filenames():
                self.open_path(path)

    def open_path(self, path):
        target = os.path.abspath(path)
        same = next((d for d in self.docs
                     if (d.path or d.source) and os.path.abspath(d.path or d.source) == target),
                    None)
        if same:
            self.switch_to(same)
            return
        if path.lower().endswith(ZPL_EXTENSIONS):
            self.import_zpl(path)
            return
        try:
            with open(path) as f:
                text = f.read()
            label = Label.from_json(text)
            values = json.loads(text).get("preview_values", {})
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.error("Could not open label", str(e))
            return
        self._remember_folder(path)
        self._add_recent(path)
        self.add_document(Document(label=label, values=values, path=path))

    def import_zpl(self, path):
        """Open a ZPL file as a new, unsaved label."""
        try:
            with open(path, "rb") as f:
                raw = f.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp1252", errors="replace")
            result = zpl_import.parse(text)
        except (OSError, zpl_import.ZplImportError) as e:
            self.error("Could not import ZPL", str(e))
            return
        self._remember_folder(path)
        self.add_document(Document(label=result.label, values=result.values, source=path))
        notes = result.warnings
        msg = f"Imported {os.path.basename(path)} at {result.label.dpi} dpi"
        if len(notes) > 1:
            self.toast.show_message(
                msg + f" · {len(notes) - 1} note{'s' if len(notes) > 2 else ''}",
                "Details", lambda: self._show_import_notes(path, notes), seconds=10)
        else:
            self.notify(msg)

    def _show_import_notes(self, path, notes):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.CLOSE,
                                text=f"Imported {os.path.basename(path)}")
        dlg.format_secondary_text("\n\n".join("• " + n for n in notes))
        dlg.run()
        dlg.destroy()

    def _document_json(self) -> str:
        d = json.loads(self.label.to_json())
        d["preview_values"] = self.values
        return json.dumps(d, indent=2)

    def save(self) -> bool:
        if not self.path:
            return self.save_as()
        try:
            with open(self.path, "w") as f:
                f.write(self._document_json())
        except OSError as e:
            self.error("Could not save", str(e))
            return False
        self.dirty = False
        self._add_recent(self.path)
        self.update_title()
        self.notify(f"Saved {os.path.basename(self.path)}")
        return True

    def save_as(self) -> bool:
        path = self.choose_save("Save Label", "label.zplinux", ("ZPLinux labels", "*.zplinux"))
        if not path:
            return False
        if not path.endswith(LABEL_EXTENSIONS):
            path += ".zplinux"
        self.path = path
        return self.save()

    def export_zpl(self):
        path = self.choose_save("Export ZPL", "label.zpl", ("ZPL files", "*.zpl"))
        if path:
            with open(path, "w") as f:
                f.write(self.current_zpl())
            self.notify(f"Exported {os.path.basename(path)}")

    def export_png(self):
        path = self.choose_save("Export PNG Preview", "label.png", ("PNG images", "*.png"))
        if path:
            render.render_png(self.label.filled(self.values), path)
            self.notify(f"Exported {os.path.basename(path)}")

    def choose_save(self, title, name, filt) -> Optional[str]:
        dlg = Gtk.FileChooserNative.new(title, self, Gtk.FileChooserAction.SAVE, None, None)
        dlg.set_do_overwrite_confirmation(True)
        named = self.path or self.doc.source
        base = os.path.splitext(os.path.basename(named))[0] if named else None
        dlg.set_current_name(base + os.path.splitext(name)[1] if base else name)
        f = Gtk.FileFilter()
        f.set_name(filt[0])
        f.add_pattern(filt[1])
        dlg.add_filter(f)
        self._set_dialog_folder(dlg)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            self._remember_folder(path)
            return path
        return None

    def _set_dialog_folder(self, dlg):
        folder = self.settings.get("last_folder")
        if folder and os.path.isdir(folder):
            dlg.set_current_folder(folder)

    def _remember_folder(self, path):
        self.settings["last_folder"] = os.path.dirname(os.path.abspath(path))
        save_settings(self.settings)

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                text="Save changes to this label?")
        dlg.format_secondary_text("Your changes will be lost if you don't save them.")
        dlg.add_buttons("Don't Save", Gtk.ResponseType.NO, "Cancel", Gtk.ResponseType.CANCEL,
                        "Save", Gtk.ResponseType.YES)
        dlg.set_default_response(Gtk.ResponseType.YES)
        r = dlg.run()
        dlg.destroy()
        if r == Gtk.ResponseType.YES:
            return self.save()
        return r == Gtk.ResponseType.NO

    def on_delete_event(self, *_):
        """Closing the window: offer to save every tab with changes."""
        for doc in [d for d in self.docs if d.dirty]:
            self.switch_to(doc)
            if not self.confirm_discard():
                return True
            doc.dirty = False
        return False

    def drop_files(self, paths, x=None, y=None):
        """Files dropped on the window or canvas: open labels, add images."""
        labels = [p for p in paths if p.lower().endswith(LABEL_EXTENSIONS + ZPL_EXTENSIONS)]
        if labels:
            for p in labels:
                self.open_path(p)
            return
        if not self.in_editor():
            self.notify("Start or open a label first, then drop images onto it.")
            return
        added = None
        for p in paths:
            el = self._image_element(p)
            if el is None:
                continue
            if added is None:
                self.checkpoint()
            cx, cy = (x, y) if x is not None else self.visible_center()
            el.x = int(max(0, cx - el.w / 2))
            el.y = int(max(0, cy - el.h / 2))
            self.label.elements.append(el)
            added = el
        if added:
            self.select(added)
            self.structure_changed()
        elif paths:
            self.notify("Only images, .zplinux and ZPL (.prn, .zpl) files can be dropped here.")

    # ---- history ------------------------------------------------------------

    def _reset_history(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.undo_key = None
        self._update_history_buttons()

    def _snapshot(self) -> str:
        index = {id(e): i for i, e in enumerate(self.label.elements)}
        idx = index.get(id(self.selected), -1)
        also = [index[id(e)] for e in self.also if id(e) in index]
        return json.dumps({"label": self.label.to_json(), "sel": idx, "also": also,
                           "values": self.values})

    def checkpoint(self, key: Optional[str] = None):
        """Call before a change. Repeated changes with the same key (typing, spinning)
        share one undo step."""
        if key is not None and key == self.undo_key:
            return
        self.undo_stack.append(self._snapshot())
        if len(self.undo_stack) > 200:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.undo_key = key
        self._update_history_buttons()

    def _restore(self, snap: str):
        d = json.loads(snap)
        self.label = Label.from_json(d["label"])
        self.values = d.get("values", self.values)
        sel = d["sel"]
        self.undo_key = None
        self.selected = self.label.elements[sel] if 0 <= sel < len(self.label.elements) else None
        self.also = [self.label.elements[i] for i in d.get("also", [])
                     if 0 <= i < len(self.label.elements)]
        self._shown_vars = None
        self.build_props()
        self.refresh_label_panel()
        self.canvas.resize_to_label()
        self.structure_changed()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self._snapshot())
            self._restore(self.undo_stack.pop())
            self._update_history_buttons()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self._snapshot())
            self._restore(self.redo_stack.pop())
            self._update_history_buttons()

    def _update_history_buttons(self):
        if not hasattr(self, "stack") or not self.in_editor():
            return
        self.lookup_action("undo").set_enabled(bool(self.undo_stack))
        self.lookup_action("redo").set_enabled(bool(self.redo_stack))

    # ---- elements -----------------------------------------------------------

    def _new_element(self, kind: str) -> Optional[Element]:
        d = dpmm(self.label.dpi)
        if kind == "text":
            return TextElement(text="Text", height=d * 5)
        if kind == "code128":
            return Code128Element(height=d * 10, module=max(1, d // 4))
        if kind == "qr":
            return QRElement(magnification=max(2, d // 2))
        if kind == "box":
            return BoxElement(w=d * 25, h=d * 12, thickness=max(2, d // 3))
        if kind == "ellipse":
            return EllipseElement(w=d * 20, h=d * 12, thickness=max(2, d // 3))
        if kind == "image":
            return self.choose_image()
        return None

    def add_element(self, kind: str, at=None):
        el = self._new_element(kind)
        if el is None:
            return
        self.checkpoint()
        cx, cy = at if at else self.visible_center()
        el.x = int(max(0, cx - (60 if at is None else 0)))
        el.y = int(max(0, cy - (30 if at is None else 0)))
        self.label.elements.append(el)
        self.select(el)
        self.structure_changed()
        if kind in ("text", "code128", "qr"):
            GLib.idle_add(self.activate_element, el)

    def choose_image(self) -> Optional[ImageElement]:
        dlg = Gtk.FileChooserNative.new("Add Image", self, Gtk.FileChooserAction.OPEN, None, None)
        f = Gtk.FileFilter()
        f.set_name("Images")
        f.add_pixbuf_formats()
        dlg.add_filter(f)
        self._set_dialog_folder(dlg)
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            return None
        return self._image_element(dlg.get_filename(), report=True)

    def _image_element(self, path, report=False) -> Optional[ImageElement]:
        try:
            info = GdkPixbuf.Pixbuf.get_file_info(path)
            if info is None or info[0] is None:
                raise ValueError("Not an image GTK can read")
            _fmt, w, h = info
            with open(path, "rb") as fh:
                raw = fh.read()
        except (OSError, ValueError, GLib.Error) as e:
            if report:
                self.error("Could not load image", str(e))
            return None
        limit = min(self.label.width_dots, self.label.height_dots) // 2
        scale = min(1.0, limit / max(w, h, 1))
        return ImageElement(png_b64=base64.b64encode(raw).decode(),
                            w=max(4, round(w * scale)), h=max(4, round(h * scale)))

    def delete_selected(self):
        sel = self.selection
        if not sel:
            return
        self.checkpoint()
        for el in sel:
            self.label.elements[:] = [e for e in self.label.elements if e is not el]
        self.select(None)
        self.structure_changed()
        what = KIND_NAMES[sel[0].kind] if len(sel) == 1 else f"{len(sel)} objects"
        self.notify(f"{what} deleted", "Undo", self.undo)

    def duplicate_selected(self):
        sel = self.selection
        if not sel:
            return
        self.checkpoint()
        copies = []
        for src in sel:
            el = element_from_dict(src.to_dict())
            el.x += dpmm(self.label.dpi) * 2
            el.y += dpmm(self.label.dpi) * 2
            self.label.elements.append(el)
            copies.append(el)
        self.set_selection(copies)
        self.structure_changed()

    def restack(self, where: str):
        sel = self.selection
        if not sel:
            return
        els = self.label.elements
        chosen = [e for e in els if any(e is s for s in sel)]
        rest = [e for e in els if not any(e is s for s in sel)]
        if where == "top":
            new = rest + chosen
        elif where == "bottom":
            new = chosen + rest
        else:
            new = list(els)
            order = range(len(new) - 2, -1, -1) if where == "up" else range(1, len(new))
            step = 1 if where == "up" else -1
            for i in order:              # each selected object passes one other object
                j = i + step
                if any(new[i] is s for s in sel) and not any(new[j] is s for s in sel):
                    new[i], new[j] = new[j], new[i]
        if all(a is b for a, b in zip(new, els)):
            return
        self.checkpoint()
        els[:] = new
        self.structure_changed()

    def align(self, where: str):
        """One object: to the label's edges and centre. Several: to each other (the
        box around them)."""
        sel = [e for e in self.selection if not e.locked]
        if not sel:
            return
        if len(sel) == 1:
            L, T, R, B = 0, 0, self.label.width_dots, self.label.height_dots
        else:
            boxes = [e.bounds() for e in sel]
            L, T = min(b[0] for b in boxes), min(b[1] for b in boxes)
            R, B = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
        self.checkpoint()
        for el in sel:
            _x, _y, w, h = el.bounds()
            if where == "left":
                el.x = L
            elif where == "hcenter":
                el.x = round((L + R - w) / 2)
            elif where == "right":
                el.x = R - w
            elif where == "top":
                el.y = T
            elif where == "vcenter":
                el.y = round((T + B - h) / 2)
            elif where == "bottom":
                el.y = B - h
        self.element_changed(self.selected)

    # ---- selection ----------------------------------------------------------

    @property
    def selection(self) -> List[Element]:
        """Everything selected, the main object last."""
        live = {id(e) for e in self.label.elements}
        sel = [e for e in self.also if id(e) in live]
        if self.selected is not None and id(self.selected) in live:
            sel.append(self.selected)
        return sel

    def is_selected(self, el) -> bool:
        return any(e is el for e in self.selection)

    def set_selection(self, els: List[Element]):
        """Select these objects; the last is the main one (its settings show)."""
        els = [e for i, e in enumerate(els) if not any(e is f for f in els[:i])]
        main = els[-1] if els else None
        changed = (main is not self.selected or len(els) - 1 != len(self.also)
                   or not all(any(e is a for a in self.also) for e in els[:-1]))
        self.also = els[:-1]
        if not changed and self.props_form is not None:
            return
        self.selected = main
        self.undo_key = None
        self.build_props()
        if main is not None:
            self.panel_stack.set_visible_child_name("element")
        self._sync_layer_selection()
        self.canvas.queue_draw()
        self.update_status()

    def toggle_selected(self, el: Element):
        """Shift-click: add an object to the selection or take it out."""
        sel = self.selection
        if any(e is el for e in sel):
            self.set_selection([e for e in sel if e is not el])
        else:
            self.set_selection(sel + [el])

    def select_all(self):
        """Everything that isn't locked."""
        self.set_selection([e for e in self.label.elements if not e.locked])

    def toggle_lock(self):
        """Lock the selection in place, or unlock it if it is all locked."""
        sel = self.selection
        if not sel:
            return
        lock = not all(e.locked for e in sel)
        self.checkpoint()
        for el in sel:
            el.locked = lock
        self._layers_sig = None
        self.refresh_layers()
        self.build_props()
        self.canvas.queue_draw()
        self.mark_dirty()
        self.notify("Locked: pick it in Layers to change or unlock it" if lock else "Unlocked")

    def select(self, el: Optional[Element]):
        """Select just this object (None: nothing)."""
        self.set_selection([el] if el is not None else [])

    def activate_element(self, el: Element):
        """Double-click or Enter: jump to the element's main field."""
        if not any(e is el for e in self.label.elements):
            return False          # queued for a tab that is no longer the current one
        if el is not self.selected:
            self.select(el)
        self.panel_stack.set_visible_child_name("element")
        w = self.props_form.first_input if self.props_form else None
        if w is not None:
            w.grab_focus()
            if isinstance(w, Gtk.Entry):
                w.select_region(0, -1)
            elif isinstance(w, Gtk.TextView):
                buf = w.get_buffer()
                buf.select_range(buf.get_start_iter(), buf.get_end_iter())
        return False

    # ---- change notifications ----------------------------------------------

    def element_changed(self, el: Element):
        """Geometry of one element changed on the canvas (drag, nudge, align)."""
        self.mark_dirty()
        if self.props_form and el is self.selected:
            self.props_form.refresh_spins("x", "y", "w", "h", "height", "block_width",
                                          "magnification")
        self._later("layers", self.refresh_layers)
        self.update_status()

    def drag_finished(self):
        self.undo_key = None

    def structure_changed(self):
        """Elements added, removed or reordered."""
        self.mark_dirty()
        self.refresh_layers()
        self.refresh_label_panel_vars()

    def mark_dirty(self):
        self.dirty = True
        self.update_title()
        self.canvas.queue_draw()
        self.refresh_zpl()

    def _later(self, key, fn, ms=120):
        """Run fn once after a short delay, coalescing repeated requests."""
        if key in self._pending:
            return
        self._pending.add(key)

        def run():
            self._pending.discard(key)
            fn()
            return False
        GLib.timeout_add(ms, run)

    # ---- layers -------------------------------------------------------------

    def describe(self, el: Element) -> str:
        if isinstance(el, (BoxElement, EllipseElement, ImageElement)):
            return f"{self.fmt(el.w)} × {self.fmt(el.h)} {UNITS[self.unit][0]}"
        return describe(el)

    def refresh_layers(self):
        els = self.label.elements
        sig = tuple((id(e), e.kind, self.describe(e), e.locked) for e in els) + (self.unit,)
        if sig != self._layers_sig:
            self._layers_sig = sig
            self._syncing_layers = True
            for c in self.layers.get_children():
                self.layers.remove(c)
            for el in reversed(els):            # front-most first
                row = Gtk.ListBoxRow()
                row.element = el
                box = Gtk.Box(spacing=10)
                box.pack_start(Gtk.Image.new_from_icon_name(KIND_ICONS[el.kind],
                                                            Gtk.IconSize.BUTTON), False, False, 0)
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                k = Gtk.Label(label=KIND_NAMES[el.kind], xalign=0)
                k.get_style_context().add_class("layer-kind")
                d = Gtk.Label(label=self.describe(el), xalign=0)
                d.set_ellipsize(Pango.EllipsizeMode.END)
                col.pack_start(k, False, False, 0)
                col.pack_start(d, False, False, 0)
                box.pack_start(col, True, True, 0)
                if el.locked:
                    lk = Gtk.Image.new_from_icon_name("changes-prevent-symbolic",
                                                      Gtk.IconSize.BUTTON)
                    lk.set_tooltip_text("Locked")
                    box.pack_end(lk, False, False, 0)
                row.add(box)
                row.show_all()
                self.layers.add(row)
            self._syncing_layers = False
        self._sync_layer_selection()

    def _sync_layer_selection(self):
        self._syncing_layers = True
        for row in self.layers.get_children():
            if self.is_selected(row.element):
                self.layers.select_row(row)
            else:
                self.layers.unselect_row(row)
        self._syncing_layers = False

    def _on_layer_selected(self, lb):
        """Click a layer to select it; Ctrl-click or Shift-click for several."""
        if self._syncing_layers:
            return
        picked = [r.element for r in lb.get_selected_rows()]
        keep = [e for e in self.selection if any(e is p for p in picked)]
        self.set_selection(keep + [e for e in picked if not any(e is k for k in keep)])

    # ---- element panel ------------------------------------------------------

    def _proxy(self, target, key_prefix):
        """Wraps an object so the first change to each field takes an undo checkpoint."""
        win = self

        class Proxy:
            def __getattr__(self, name):
                return getattr(target, name)

            def __setattr__(self, name, value):
                if getattr(target, name) != value:
                    win.checkpoint(f"{key_prefix}{id(target)}:{name}")
                    setattr(target, name, value)
        return Proxy()

    def build_props(self):
        child = self.props_box.get_child()
        if child:
            self.props_box.remove(child)
        el = self.selected
        if len(self.selection) > 1:
            # several objects: what applies to all of them
            sel = self.selection
            form = PropertyForm(lambda attr: None, self.unit, self.label.dpi)
            form.section(f"{len(sel)} objects selected")
            form.hint("Shift-click an object to add it or take it out; drag on empty space "
                      "to select a group. Align lines them up with each other.")
            self._layout_tools(form, any(e.can_rotate() for e in sel))
            self.props_form = form
            self.props_box.add(form)
            self.props_box.show_all()
            return
        if el is None:
            self.props_form = None
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, valign=Gtk.Align.CENTER,
                          margin=28)
            box.get_style_context().add_class("empty-state")
            img = Gtk.Image.new_from_icon_name("input-mouse-symbolic", Gtk.IconSize.DIALOG)
            img.set_pixel_size(64)
            box.pack_start(img, False, False, 0)
            t = Gtk.Label(label="Nothing selected")
            t.get_style_context().add_class("template-name")
            box.pack_start(t, False, False, 0)
            hint = dim_label("Click something on the label to edit it, or add an element "
                             "from Insert on the left.")
            hint.set_justify(Gtk.Justification.CENTER)
            hint.set_xalign(0.5)
            box.pack_start(hint, False, False, 0)
            self.props_box.add(box)
            self.props_box.show_all()
            return

        def on_change(attr):
            if attr in ("text", "data"):
                self.refresh_label_panel_vars()
                self._later("layers", self.refresh_layers)
            self.mark_dirty()
            self.update_status()
        form = PropertyForm(on_change, self.unit, self.label.dpi)
        p = self._proxy(el, "el")
        maxw, maxh = self.label.width_dots, self.label.height_dots

        if isinstance(el, TextElement):
            form.section("Text")
            form.textarea(None, p, "text")
            form.hint(VARIABLES_HINT)
            form.section("Font")
            self._font_rows(form, el, p)
            form.switch("White on black", p, "reverse")
            form.section("Text box")
            wrap = SimpleNamespace(on=bool(el.block_width))

            def toggled(_attr):
                self.checkpoint()
                el.block_width = (el.measured_w or 300) if wrap.on else 0
                self.mark_dirty()
                GLib.idle_add(self.build_props)
            normal = form.on_change
            form.on_change = toggled
            form.switch("Wrap in a box", wrap, "on")
            form.on_change = normal
            if el.block_width:
                form.length("Box width", p, "block_width", 10, maxw)
                form.spin("Max lines", p, "max_lines", 1, 50)
                form.toggle_group("Alignment", p, "align", [
                    ("L", "format-justify-left-symbolic", "Left"),
                    ("C", "format-justify-center-symbolic", "Center"),
                    ("R", "format-justify-right-symbolic", "Right"),
                    ("J", "format-justify-fill-symbolic", "Justify")])
            else:
                form.hint("Turn on to wrap long text, use several lines, or center it.")
        elif isinstance(el, Code128Element):
            form.section("Barcode")
            self._barcode_type_row(form, el)
            form.entry(None, p, "data", "e.g. ABC-123 or {sku}")
            form.hint("Code 128: letters, digits and symbols (printable ASCII).")
            form.hint(VARIABLES_HINT)
            form.section("Size")
            form.length("Bar height", p, "height", 10, maxh)
            form.spin("Narrow bar (dots)", p, "module", 1, 10)
            form.switch("Show text below", p, "show_text")
            form.length("Text size", p, "text_size", 0, 1000)
            form.hint("Text size 0 lets the printer pick its usual small text.")
        elif isinstance(el, BarcodeElement):
            self._barcode_props(form, el, p, maxh)
        elif isinstance(el, QRElement):
            form.section("QR code")
            self._barcode_type_row(form, el)
            form.entry(None, p, "data", "Text, a link, or {variable}")
            form.hint(VARIABLES_HINT)
            form.section("Size")
            form.spin("Module size (dots)", p, "magnification", 1, 10)
            form.combo("Error correction", p, "error_correction",
                       [("L", "Low (7%)"), ("M", "Medium (15%)"), ("Q", "Quartile (25%)"),
                        ("H", "High (30%)")])
        elif isinstance(el, BoxElement):
            form.section("Box")
            form.length("Width", p, "w", 1, maxw)
            form.length("Height", p, "h", 1, maxh)
            form.length("Line thickness", p, "thickness", 1, 500)
            form.spin("Corner rounding (0–8)", p, "rounding", 0, 8)
            form.switch("Filled", p, "filled")
            form.hint("For a line, make the height equal to the line thickness.")
        elif isinstance(el, EllipseElement):
            form.section("Ellipse")
            form.length("Width", p, "w", 3, maxw)
            form.length("Height", p, "h", 3, maxh)
            form.length("Line thickness", p, "thickness", 1, 500)
            form.switch("Filled", p, "filled")
        elif isinstance(el, ImageElement):
            form.section("Image")
            form.length("Width", p, "w", 4, maxw)
            form.length("Height", p, "h", 4, maxh)
            form.switch("Keep proportions", p, "keep_aspect")
            form.spin("Black threshold", p, "threshold", 1, 255)
            form.hint("Thermal printers only print black dots: pixels darker than the "
                      "threshold print black.")

        form.section("Position")
        lock = SimpleNamespace(on=el.locked)

        def lock_toggled(_attr):
            if lock.on != el.locked:
                GLib.idle_add(lambda: self.toggle_lock() and False)
        normal = form.on_change
        form.on_change = lock_toggled
        form.switch("Locked", lock, "on")
        form.on_change = normal
        if el.locked:
            form.hint("Locked: it can't be clicked or dragged on the label. Pick it in "
                      "Layers to change it.")
        # objects may sit partly or wholly off the label; only the part on it prints
        form.length("X", p, "x", -10 * maxw, 10 * maxw)
        form.length("Y", p, "y", -10 * maxh, 10 * maxh)
        if el.can_rotate() and not isinstance(el, (BoxElement, EllipseElement)):
            form.combo("Rotation", p, "rotation",
                       [(0, "0°"), (90, "90° clockwise"), (180, "180°"),
                        (270, "90° counterclockwise")])
        self._layout_tools(form, el.can_rotate())

        self.props_form = form
        self.props_box.add(form)
        self.props_box.show_all()

    # ---- label panel --------------------------------------------------------

    # ---- text fonts ---------------------------------------------------------

    def _font_rows(self, form, el, p):
        """Font list; font 0 takes any size, the printer fonts A-H whole steps."""
        fonts = [("0", "0 – scalable (CG Triumvirate)")] + [
            (k, f"{bf.name} – {bf.cell_h} × {bf.cell_w} dots" + (", capitals" if bf.capitals
                                                                 else ""))
            for k, bf in BITMAP_FONTS.items()]
        combo = Gtk.ComboBoxText()
        for key, text in fonts:
            combo.append(key, text)
        combo.set_active_id(el.font if el.font in BITMAP_FONTS else "0")

        def changed(c):
            key = c.get_active_id()
            if key is None or key == el.font:
                return
            h, _w = el.font_size()
            self.checkpoint()
            el.font = key
            bf = el.bitmap_font()
            if bf:          # snap to whole steps, keeping about the same size
                el.height = bf.cell_h * magnification(h, bf.cell_h)
                el.width = 0
            self.mark_dirty()
            GLib.idle_add(self.build_props)
        combo.connect("changed", changed)
        form.widget(combo, "Font")
        bf = el.bitmap_font()
        if bf is None:
            form.length("Size", p, "height", 10, 2000)
            form.length("Character width", p, "width", 0, 2000)
            form.hint("Character width 0 keeps normal proportions.")
            return
        h, w = el.font_size()
        steps = SimpleNamespace(tall=h // bf.cell_h, wide=w // bf.cell_w)

        def step_changed(attr):
            if attr == "tall":
                p.height = bf.cell_h * steps.tall
            else:
                p.width = bf.cell_w * steps.wide
            size_note.set_text(note())
        normal = form.on_change
        form.on_change = step_changed
        form.spin("Height (× cell)", steps, "tall", 1, 10)
        form.spin("Width (× cell)", steps, "wide", 1, 10)
        form.on_change = normal

        def note():
            hh, ww = el.font_size()
            d = dpmm(self.label.dpi)
            pitch = (ww + bf.gap * ww // bf.cell_w) / d
            return (f"Characters are {hh / d:.1f} mm tall and {pitch:.1f} mm apart. A font "
                    "built into the printer: it prints exactly the size shown.")
        size_note = dim_label(note())
        form.widget(size_note)

    # ---- barcode types ------------------------------------------------------

    def _barcode_type_row(self, form, el):
        """The Type list at the top of every barcode and QR code: Code 128, QR and the
        symbology table, grouped by separators."""
        combo = Gtk.ComboBoxText()
        combo.set_row_separator_func(lambda model, it: model[it][1] == "-")
        combo.append("code128", "Code 128")
        last = "1D"
        for sym in symbology.SYMBOLOGIES:
            if sym.group != last:
                combo.append("-", "-")
                if sym.group == "2D":
                    combo.append("qr", "QR code")
                last = sym.group
            combo.append(sym.key, sym.name)
        combo.set_active_id(barcode_type(el))
        combo.set_wrap_width(1)
        combo.connect("changed", lambda c: c.get_active_id() not in (None, "-")
                      and GLib.idle_add(self.change_barcode_type, el, c.get_active_id()))
        form.widget(combo, "Type")

    def change_barcode_type(self, el, key: str):
        """Swap a barcode for another type in the same place, keeping its data unless
        that was just the old type's example."""
        if key == barcode_type(el) or not any(e is el for e in self.label.elements):
            return False
        examples = {s.example for s in symbology.SYMBOLOGIES} | {
            Code128Element().data, QRElement().data, ""}
        spec = symbology.get(key)
        d = dpmm(self.label.dpi)
        if key == "code128":
            data = Code128Element().data
        elif key == "qr":
            data = QRElement().data
        else:
            data = spec.example
        if el.data not in examples:
            data = el.data
        module = getattr(el, "module", max(1, d // 4))
        if key == "code128":
            new = Code128Element(data=data, height=getattr(el, "height", d * 10), module=module)
        elif key == "qr":
            new = QRElement(data=data, magnification=getattr(el, "magnification", max(2, d // 2)))
        else:
            two_d = spec.shape in ("matrix", "maxicode")
            new = BarcodeElement(symbology=key, data=data,
                                 module=max(2, d // 2) if two_d else module,
                                 height=getattr(el, "height", d * 10) if not two_d else d * 10,
                                 show_text=getattr(el, "show_text", True))
            if spec.shape == "stacked":
                # a row height, not a bar height: PDF417 rows are about 3 modules tall
                new.height = new.module * (8 if key in ("codablock", "code49") else 3)
            if spec.modes:
                new.mode = spec.modes[0][0]
        new.x, new.y = el.x, el.y
        self.checkpoint()
        i = next(i for i, e in enumerate(self.label.elements) if e is el)
        self.label.elements[i] = new
        self.selected = new
        self.props_form = None
        self.build_props()
        self.structure_changed()
        self.canvas.queue_draw()
        return False

    def _barcode_props(self, form, el, p, maxh):
        spec = symbology.get(el.symbology)
        form.section("Barcode")
        self._barcode_type_row(form, el)
        form.entry(None, p, "data", spec.example if spec else "")
        if spec is None:
            form.hint(f"Unknown barcode type '{el.symbology}'.")
            return
        form.hint(spec.hint)
        form.hint(VARIABLES_HINT)
        if not zint.available():
            form.hint("Install zint to see this barcode in the preview; it prints without it.")
        opts = spec.options
        stacked = spec.shape == "stacked"
        form.section("Size")
        if "height" in opts:
            form.length("Row height" if stacked else "Bar height", p, "height", 2, maxh)
        if spec.shape != "maxicode":
            form.spin("Module size (dots)" if spec.shape in ("matrix", "stacked", "scaled")
                      else "Narrow bar (dots)", p, "module", 1, 10)
        if "ratio" in opts:
            form.spin("Wide to narrow ratio", p, "ratio", 2.0, 3.0, step=0.1, digits=1)
        if "columns" in opts:
            form.spin("Columns (0 = automatic)", p, "columns", 0, 144)
        if "rows" in opts:
            form.spin("Rows (0 = automatic)", p, "rows", 0, 144)
        if "security" in opts:
            form.spin("Error correction level", p, "security", 0, 8)
        if "mode" in opts and spec.modes:
            form.combo("Variant" if el.symbology == "databar" else "Mode", p, "mode",
                       list(spec.modes))
        if "show_text" in opts:
            form.switch("Show text below", p, "show_text")
        if "check" in opts:
            form.switch("Add check digit", p, "check")

    def _layout_tools(self, form, rotatable: bool):
        """Align buttons and the Arrange row, for one object or several."""
        align = Gtk.Box(spacing=8, homogeneous=True)
        tips = {"left": "Align to left edge", "hcenter": "Center horizontally",
                "right": "Align to right edge", "top": "Align to top edge",
                "vcenter": "Center vertically", "bottom": "Align to bottom edge"}
        for group in (("left", "hcenter", "right"), ("top", "vcenter", "bottom")):
            g = Gtk.Box(homogeneous=True)
            g.get_style_context().add_class("linked")
            for where in group:
                g.pack_start(icon_button(f"zplinux-align-{where}-symbolic", tips[where],
                                         lambda w=where: self.align(w), flat=False), True, True, 0)
            align.pack_start(g, True, True, 0)
        form.widget(align)

        form.section("Arrange")
        arr = Gtk.Box(spacing=6)
        order = Gtk.Box()
        order.get_style_context().add_class("linked")
        for icon, tip, where in (("go-top-symbolic", "Bring to front", "top"),
                                 ("go-up-symbolic", "Bring forward", "up"),
                                 ("go-down-symbolic", "Send backward", "down"),
                                 ("go-bottom-symbolic", "Send to back", "bottom")):
            order.pack_start(icon_button(icon, tip, lambda w=where: self.restack(w), flat=False),
                             False, False, 0)
        arr.pack_start(order, False, False, 0)
        arr.pack_start(icon_button("edit-copy-symbolic", "Duplicate (Ctrl+D)",
                                   self.duplicate_selected, flat=False), False, False, 0)
        if rotatable:
            arr.pack_start(icon_button("object-rotate-right-symbolic",
                                       "Rotate 90° clockwise (Ctrl+R)",
                                       lambda: self.rotate_selected(90), flat=False),
                           False, False, 0)
        delete = icon_button("user-trash-symbolic", "Delete (Del)", self.delete_selected, flat=False)
        delete.get_style_context().add_class("destructive-action")
        arr.pack_end(delete, False, False, 0)
        form.widget(arr)

    def rotate_selected(self, step: int = 90):
        """Turn the selected objects, each about its own centre."""
        sel = [e for e in self.selection if e.can_rotate() and not e.locked]
        if not sel:
            if self.selected is not None:
                self.notify(f"{KIND_NAMES.get(self.selected.kind, 'This code')} prints "
                            "upright; it scans from any side.")
            return
        self.checkpoint()
        for el in sel:
            bx, by, bw, bh = el.bounds()
            turn(el, step)
            _x, _y, nw, nh = el.bounds()
            el.x, el.y = round(bx + (bw - nw) / 2), round(by + (bh - nh) / 2)
        self.build_props()
        self.element_changed(self.selected)

    def set_orientation(self, degrees: int):
        """How the design is turned when printed (onto label stock the other way round).
        The design itself doesn't change."""
        lab = self.label
        if degrees % 360 == lab.orientation:
            return False
        self.checkpoint()
        lab.orientation = degrees % 360
        self.size_info.set_text(self._size_text())
        self.mark_dirty()
        return False

    def refresh_label_panel(self):
        child = self.label_box.get_child()
        if child:
            self.label_box.remove(child)
        lab = self.label
        p = self._proxy(lab, "label")
        imperial = self.unit == "in"

        def changed(attr):
            self.mark_dirty()
            if attr in ("width_mm", "height_mm", "dpi"):
                self.settings.update(last_width_mm=lab.width_mm, last_height_mm=lab.height_mm,
                                     last_dpi=lab.dpi)
                save_settings(self.settings)
                self.size_info.set_text(self._size_text())
                self.canvas.resize_to_label()
                if attr == "dpi":
                    self.build_props()
                self.update_status()

        form = PropertyForm(changed, self.unit, lab.dpi)
        form.section("Label size")
        preset = Gtk.ComboBoxText()
        preset.append("custom", "Custom")
        for i, (name, _w, _h) in enumerate(SIZE_PRESETS):
            preset.append(str(i), name)
        cur = next((str(i) for i, (_n, w, h) in enumerate(SIZE_PRESETS)
                    if abs(w - lab.width_mm) < 0.01 and abs(h - lab.height_mm) < 0.01), "custom")
        preset.set_active_id(cur)
        form.widget(preset, "Preset")
        if imperial:
            to_in, from_in = (lambda v: v / MM_PER_INCH), (lambda v: v * MM_PER_INCH)
            wspin = form._spin("Width (in)", p, "width_mm", 0.2, 12, 0.05, 2, to_in, from_in)
            hspin = form._spin("Height (in)", p, "height_mm", 0.2, 40, 0.05, 2, to_in, from_in)
        else:
            wspin = form.spin("Width (mm)", p, "width_mm", 5, 300, step=0.5, digits=1)
            hspin = form.spin("Height (mm)", p, "height_mm", 5, 1000, step=0.5, digits=1)

        def on_preset(c):
            if c.get_active_id() in (None, "custom"):
                return
            _n, w, h = SIZE_PRESETS[int(c.get_active_id())]
            wspin.set_value(w / MM_PER_INCH if imperial else w)
            hspin.set_value(h / MM_PER_INCH if imperial else h)
        preset.connect("changed", on_preset)
        form.combo("Resolution", p, "dpi", [(d, f"{d} dpi") for d in DPI_CHOICES])
        turn_print = Gtk.ComboBoxText()
        for deg, text in ((0, "No"), (90, "90° clockwise"), (180, "180°"),
                          (270, "90° counterclockwise")):
            turn_print.append(str(deg), text)
        turn_print.set_active_id(str(lab.orientation % 360))
        turn_print.connect("changed", lambda c: GLib.idle_add(
            self.set_orientation, int(c.get_active_id())))
        form.widget(turn_print, "Rotate when printing")
        form.hint("For a landscape design on labels that feed the other way, such as "
                  "6 × 4 in on 4 × 6 in stock.")
        self.size_info = dim_label(self._size_text())
        form.widget(self.size_info)

        form.section("Printing")
        form.spin("Copies", p, "copies", 1, 9999)
        form.spin("Darkness adjust", p, "darkness", -30, 30)
        form.hint("0 keeps the printer's own darkness setting.")

        form.section("Date, time and counter")
        form.hint("Type {date}, {time} or {counter} in any text or barcode. They are "
                  "filled in when the label prints; the counter goes up by one step "
                  "for every label.")
        now = datetime.datetime.now()
        form.combo("Date", p, "date_format",
                   [(f, now.strftime(f)) for f, _ex in DATE_FORMATS])
        form.combo("Time", p, "time_format",
                   [(f, now.strftime(f)) for f, _ex in TIME_FORMATS])
        self.counter_spin = form.spin("Next number", p, "counter_next", -999999, 99999999)
        form.spin("Step", p, "counter_step", 1, 1000)
        form.spin("Digits (pad with zeros)", p, "counter_digits", 0, 12)

        form.section("Variables")
        self.vars_grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        form.widget(self.vars_grid)
        csvb = Gtk.Button(label="Print a Batch from a File…")
        csvb.set_action_name("win.print-csv")
        form.widget(csvb)
        self._shown_vars = None
        self.label_box.add(form)
        self.refresh_label_panel_vars()
        self.label_box.show_all()

    def refresh_label_panel_vars(self):
        grid = getattr(self, "vars_grid", None)
        if grid is None:
            return
        names = self.label.manual_variables()
        if self._shown_vars == names:
            return
        self._shown_vars = names
        for c in grid.get_children():
            grid.remove(c)
        if not names:
            grid.attach(dim_label("Type {name} in any text, barcode or QR field to create a "
                                  "variable. Fill it in here, or print one label per row of a "
                                  "CSV or spreadsheet file whose column headers match."), 0, 0, 2, 1)
        entries = []
        for i, name in enumerate(names):
            grid.attach(Gtk.Label(label=name, xalign=0), 0, i, 1, 1)
            e = Gtk.Entry(text=str(self.values.get(name, "")), hexpand=True)

            def changed(w, n=name):
                if self.values.get(n, "") == w.get_text():
                    return
                self.checkpoint(f"var:{n}")
                self.values[n] = w.get_text()
                self.mark_dirty()
            e.connect("changed", changed)
            # Enter moves on to the next variable (from the last, back to the first)
            e.connect("activate", lambda _w, i=i: self._focus_variable(entries, i + 1))
            e.connect("focus-in-event", lambda w, _ev: scroll_into_view(self.label_box, w) and False)
            entries.append(e)
            grid.attach(e, 1, i, 1, 1)
        grid.show_all()

    def _focus_variable(self, entries, i):
        target = entries[i % len(entries)]
        target.grab_focus()
        scroll_into_view(self.label_box, target)

    def _size_text(self):
        lab = self.label
        text = (f"{lab.width_dots} × {lab.height_dots} printer dots · "
                f"{lab.width_mm:.1f} × {lab.height_mm:.1f} mm · "
                f"{lab.width_mm / MM_PER_INCH:.2f} × {lab.height_mm / MM_PER_INCH:.2f} in")
        if lab.orientation % 360:
            w, h = lab.stock_mm
            text += (f"\nPrints turned {lab.orientation}° onto {w:.1f} × {h:.1f} mm "
                     f"({w / MM_PER_INCH:.2f} × {h / MM_PER_INCH:.2f} in) labels.")
        return text

    # ---- refresh ------------------------------------------------------------

    def update_title(self):
        self.header.set_subtitle(("• " if self.dirty else "") + self.doc.name())
        if self.doc in self.docs:
            self._update_tab(self.doc)

    def current_zpl(self) -> str:
        return zpl.label_zpl(self.label, self.values)

    def refresh_zpl(self):
        def do():
            text = self.current_zpl()
            if len(text) > 200_000:
                text = text[:200_000] + "\n… (truncated in this view)"
            self.zpl_view.get_buffer().set_text(text)
        self._later("zpl", do, 150)

    def copy_zpl(self):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.current_zpl(), -1)
        self.notify("ZPL copied to the clipboard")

    def notify(self, msg: str, action: Optional[str] = None, cb: Optional[Callable] = None):
        self.toast.show_message(msg, action, cb)

    def fmt(self, dots: float) -> str:
        _label, digits = UNITS[self.unit]
        return f"{to_unit(dots, self.unit, self.label.dpi):.{digits}f}"

    def update_status(self):
        lab = self.label
        u = UNITS[self.unit][0]
        parts = [f"{self.fmt(lab.width_dots)} × {self.fmt(lab.height_dots)} {u} · {lab.dpi} dpi"]
        if self._cursor:
            parts.append(f"Pointer {self.fmt(self._cursor[0])}, {self.fmt(self._cursor[1])}")
        if len(self.selection) > 1:
            parts.append(f"{len(self.selection)} objects selected")
        elif self.selected:
            _bx, _by, bw, bh = self.selected.bounds()
            parts.append(f"{KIND_NAMES[self.selected.kind]} {self.fmt(bw)} × {self.fmt(bh)}")
            where = zpl.placement(self.selected, lab)
            if where == "cut":
                parts.append("partly off the label")
            elif where == "outside":
                parts.append("off the label, won't print")
        self.status.set_text("   ·   ".join(parts))

    def pointer_moved(self, x, y):
        lab = self.label
        inside = x is not None and 0 <= x <= lab.width_dots and 0 <= y <= lab.height_dots
        self._cursor = (x, y) if inside else None
        self.update_status()

    # ---- zoom ---------------------------------------------------------------

    def set_zoom(self, z: float):
        self.canvas.zoom = max(0.1, min(8.0, z))
        self.zoom_btn.set_label(f"{round(self.canvas.zoom * 100)}%")
        self.canvas.resize_to_label()
        self.canvas.center_on_label()

    def zoom_fit(self):
        a = self.scroller.get_allocation()
        if a.width < 50:
            return False
        z = min((a.width - 80) / self.label.width_dots, (a.height - 120) / self.label.height_dots)
        self.set_zoom(z)
        return False

    def visible_center(self):
        """Centre of the visible part of the label, staggered so new elements don't stack."""
        lw, lh = self.label.width_dots, self.label.height_dots
        h = self.scroller.get_hadjustment()
        v = self.scroller.get_vadjustment()
        x0, y0 = self.canvas.to_dots(h.get_value(), v.get_value())
        x1, y1 = self.canvas.to_dots(h.get_value() + h.get_page_size(),
                                     v.get_value() + v.get_page_size())
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(lw, x1), min(lh, y1)
        step = dpmm(self.label.dpi) * 4 * (len(self.label.elements) % 5)
        return (int(min(max(0, (x0 + x1) / 2 + step), lw - 50)),
                int(min(max(0, (y0 + y1) / 2 + step), lh - 30)))

    def on_scroll(self, widget, ev):
        if ev.state & Gdk.ModifierType.CONTROL_MASK:
            ok, _dx, dy = ev.get_scroll_deltas()
            up = ev.direction == Gdk.ScrollDirection.UP or (ok and dy < 0)
            self.set_zoom(self.canvas.zoom * (1.12 if up else 1 / 1.12))
            return True
        return False

    # ---- context menu & keys ------------------------------------------------

    def context_menu(self, ev, el: Optional[Element]):
        menu = Gtk.Menu()

        def item(text, cb):
            it = Gtk.MenuItem(label=text)
            it.connect("activate", lambda *_: cb())
            menu.append(it)

        def sep():
            menu.append(Gtk.SeparatorMenuItem())

        if el is not None:
            if isinstance(el, (TextElement, Code128Element, QRElement)):
                item("Edit Content", lambda: self.activate_element(el))
                sep()
            item("Duplicate", self.duplicate_selected)
            item("Rotate 90°", lambda: self.rotate_selected(90))
            item("Unlock" if el.locked else "Lock", self.toggle_lock)
            item("Delete", self.delete_selected)
            sep()
            item("Bring to Front", lambda: self.restack("top"))
            item("Send to Back", lambda: self.restack("bottom"))
            sep()
            item("Center Horizontally", lambda: self.align("hcenter"))
            item("Center Vertically", lambda: self.align("vcenter"))
        else:
            x, y = self.canvas.to_dots(ev.x, ev.y)
            at = (int(max(0, x)), int(max(0, y)))
            for kind, text in TOOLS:
                item(f"Add {text} Here", lambda k=kind: self.add_element(k, at))
        menu.show_all()
        menu.attach_to_widget(self.canvas, None)
        menu.popup_at_pointer(ev)

    def on_key(self, widget, ev):
        if not self.in_editor():
            if Gdk.keyval_name(ev.keyval) == "Escape" and self.docs:
                self.show_editor()
                return True
            return False
        key = Gdk.keyval_name(ev.keyval) or ""
        # Leave typing keys alone while an input field has focus; Escape returns to the canvas.
        if isinstance(self.get_focus(), (Gtk.Entry, Gtk.TextView, Gtk.SpinButton)):
            if key == "Escape":
                self.canvas.grab_focus()
                return True
            return False
        if ev.state & Gdk.ModifierType.CONTROL_MASK and key.lower() == "a":
            self.select_all()
            return True
        if ev.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        if key in ("Delete", "BackSpace"):
            self.delete_selected()
            return True
        if key == "Escape":
            self.select(None)
            return True
        if key in ("Tab", "ISO_Left_Tab") and self.label.elements:
            els = self.label.elements
            i = els.index(self.selected) if self.selected in els else -1
            i = (i - 1) % len(els) if (shift or key == "ISO_Left_Tab") else (i + 1) % len(els)
            self.select(els[i])
            return True
        if key in ("Return", "KP_Enter") and self.selected:
            self.activate_element(self.selected)
            return True
        moves = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}
        if key in moves and self.selected:
            step = dpmm(self.label.dpi) if shift else 1
            dx, dy = moves[key]
            movable = [e for e in self.selection if not e.locked]
            if not movable:
                self.notify("Locked objects stay where they are; unlock with Ctrl+L.")
                return True
            self.checkpoint("nudge")
            for el in movable:
                el.x += dx * step
                el.y += dy * step
            self.element_changed(self.selected)
            return True
        return False

    # ---- printing -----------------------------------------------------------

    def print_label(self):
        missing = [v for v in self.label.manual_variables() if not self.values.get(v)]
        if missing and not self.ask("Some variables are empty",
                                    f"{', '.join(missing)} will print as written. Print anyway?",
                                    "Print"):
            return
        n = self.label.copies
        if self._send(self.current_zpl(), f"{n} label{'s' if n != 1 else ''}"):
            self._advance_counter(zpl.labels_printed(self.label))

    def print_csv(self):
        """Print one label per row of a CSV, Excel or LibreOffice file."""
        dlg = Gtk.FileChooserNative.new("Print a Batch", self, Gtk.FileChooserAction.OPEN,
                                        None, None)
        for name, exts in (("Spreadsheets and CSV", datafile.EXTENSIONS),
                           ("CSV files", (".csv", ".tsv", ".txt")),
                           ("Excel (.xlsx)", (".xlsx", ".xlsm")),
                           ("LibreOffice / OpenOffice (.ods)", (".ods",))):
            f = Gtk.FileFilter()
            f.set_name(name)
            for ext in exts:
                f.add_pattern("*" + ext)
                f.add_pattern("*" + ext.upper())
            dlg.add_filter(f)
        self._set_dialog_folder(dlg)
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            return
        path = dlg.get_filename()
        try:
            rows = datafile.read_rows(path)
        except datafile.DataFileError as e:
            self.error("Could not read the file", str(e))
            return
        if not rows:
            self.error("Nothing to print", "The file has no data rows below its headers.")
            return
        needed = self.label.manual_variables()
        cols = [c for c in rows[0].keys() if c]
        missing = [v for v in needed if v not in cols]
        copies = self.label.copies
        detail = (f"{len(rows)} row{'s' if len(rows) != 1 else ''} × {copies} "
                  f"cop{'y' if copies == 1 else 'ies'} to {self.printer.title()} "
                  f"({self.printer.describe()}).\n"
                  f"Columns: {', '.join(cols)}")
        if missing:
            detail += ("\n\nNot in the file (the values from the Label tab are used): "
                       + ", ".join(missing))
        if not self.ask(f"Print {len(rows) * copies} labels?", detail, "Print"):
            return
        merged = [{**self.values, **{k: (v or "") for k, v in r.items() if k}} for r in rows]
        if self._send(zpl.batch_zpl(self.label, merged),
                      f"{len(rows)} labels from {os.path.basename(path)}"):
            self._advance_counter(zpl.labels_printed(self.label, len(rows)))

    def _advance_counter(self, printed: int):
        """After printing, {counter} carries on from the next number."""
        lab = self.label
        if not lab.uses_counter():
            return
        lab.counter_next += printed * lab.counter_step
        if getattr(self, "counter_spin", None) is not None:
            self.counter_spin.set_value(lab.counter_next)
        self.mark_dirty()

    def _send(self, data: str, what: str):
        """Send a print job; True when it went out."""
        cfg = self.printer
        if cfg.method == "file" and not cfg.file_path:
            path = self.choose_save("Save Print Job", "job.zpl", ("ZPL files", "*.zpl"))
            if not path:
                return False
            cfg = printing.PrinterConfig(name=cfg.name, method="file", file_path=path)
        if not self._preflight(cfg):
            return False
        try:
            where = printing.send(data, cfg)
        except printing.PrintError as e:
            hint = ""
            if cfg.method == "network" and cfg.host in ("127.0.0.1", "localhost"):
                hint = ("\n\nTesting without a printer? Start the fake printer first:\n"
                        + FAKE_PRINTER_CMD)
            self.error("Printing failed", str(e) + hint)
            return False
        except OSError as e:
            self.error("Printing failed", str(e))
            return False
        self.notify(f"Sent {what} to {cfg.title()} ({where})")
        return True

    def printer_dialog(self):
        dlg = Gtk.Dialog(title="Printers", transient_for=self, modal=True, use_header_bar=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class(
            "suggested-action")
        # Edit copies; nothing changes until Save.
        printers = [printing.PrinterConfig.from_dict(p.to_dict()) for p in self.printers]
        in_use = printers[next(i for i, p in enumerate(self.printers) if p is self.printer)]
        state = SimpleNamespace(current=None, default=printers[self.default_printer],
                                loading=False)

        body = Gtk.Box(spacing=16, margin=16)
        dlg.get_content_area().add(body)

        # left: the saved printers
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.pack_start(side, False, False, 0)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=240,
                                shadow_type=Gtk.ShadowType.IN)
        sw.set_size_request(250, -1)
        sw.add(listbox)
        side.pack_start(sw, True, True, 0)
        tools = Gtk.Box()
        tools.get_style_context().add_class("linked")
        side.pack_start(tools, False, False, 0)

        # right: the selected printer
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, hexpand=True)
        body.pack_start(grid, True, True, 0)
        name = Gtk.Entry(hexpand=True, activates_default=True,
                         placeholder_text="e.g. Warehouse GK420d")
        grid.attach(Gtk.Label(label="Name", xalign=0), 0, 0, 1, 1)
        grid.attach(name, 1, 0, 1, 1)
        method = Gtk.ComboBoxText()
        for m in printing.METHODS:
            method.append(m, printing.METHOD_LABELS[m])
        grid.attach(Gtk.Label(label="Connection", xalign=0), 0, 1, 1, 1)
        grid.attach(method, 1, 1, 1, 1)
        stack = Gtk.Stack(vhomogeneous=False)
        grid.attach(stack, 0, 2, 2, 1)

        g = Gtk.Grid(column_spacing=10, row_spacing=10)
        host = Gtk.Entry(hexpand=True, activates_default=True,
                         placeholder_text="e.g. 192.168.1.50")
        port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        g.attach(Gtk.Label(label="Address", xalign=0), 0, 0, 1, 1)
        g.attach(host, 1, 0, 1, 1)
        g.attach(Gtk.Label(label="Port", xalign=0), 0, 1, 1, 1)
        g.attach(port, 1, 1, 1, 1)
        g.attach(dim_label("Zebra network printers listen on port 9100. To test without a "
                           f"printer, run  {FAKE_PRINTER_CMD}  and use 127.0.0.1."), 0, 2, 2, 1)
        stack.add_named(g, "network")

        g = Gtk.Grid(column_spacing=10, row_spacing=10)
        dev = Gtk.ComboBoxText.new_with_entry()
        found = printing.usb_devices()
        for d in found:
            dev.append_text(d)
        dev.set_hexpand(True)
        g.attach(Gtk.Label(label="Device", xalign=0), 0, 0, 1, 1)
        g.attach(dev, 1, 0, 1, 1)
        g.attach(dim_label(("Found: " + ", ".join(found)) if found else
                           "No USB printer found right now (/dev/usb/lp*). Writing to it "
                           "needs membership of the 'lp' group."), 0, 1, 2, 1)
        stack.add_named(g, "device")

        g = Gtk.Grid(column_spacing=10, row_spacing=10)
        queue = Gtk.ComboBoxText.new_with_entry()
        for q in printing.cups_queues():
            queue.append_text(q)
        queue.set_hexpand(True)
        g.attach(Gtk.Label(label="Printer", xalign=0), 0, 0, 1, 1)
        g.attach(queue, 1, 0, 1, 1)
        g.attach(dim_label("Sends ZPL unchanged with 'lp -o raw'. Set the queue up as a "
                           "Raw printer (no driver) so CUPS does not convert it."), 0, 1, 2, 1)
        stack.add_named(g, "cups")

        g = Gtk.Grid(column_spacing=10, row_spacing=10)
        fentry = Gtk.Entry(hexpand=True, placeholder_text="Ask each time")
        g.attach(Gtk.Label(label="File", xalign=0), 0, 0, 1, 1)
        g.attach(fentry, 1, 0, 1, 1)
        g.attach(dim_label("Writes print jobs to a .zpl file instead of a printer."), 0, 1, 2, 1)
        stack.add_named(g, "file")

        default = Gtk.CheckButton(label="Default printer")
        grid.attach(default, 0, 3, 2, 1)
        test_row = Gtk.Box(spacing=10)
        test = Gtk.Button(label="Check Printer")
        result = dim_label("")
        result.set_selectable(True)
        test_row.pack_start(test, False, False, 0)
        test_row.pack_start(result, True, True, 0)
        grid.attach(test_row, 0, 4, 2, 1)

        def store():
            """Copy the form into the printer being edited."""
            cfg = state.current
            if cfg is None or state.loading:
                return
            cfg.name = name.get_text().strip()
            cfg.method = method.get_active_id()
            cfg.host = host.get_text().strip()
            cfg.port = port.get_value_as_int()
            cfg.device = dev.get_child().get_text().strip()
            cfg.cups_queue = queue.get_child().get_text().strip()
            cfg.file_path = fentry.get_text().strip()

        def load(cfg):
            state.loading = True
            state.current = cfg
            name.set_text(cfg.name)
            method.set_active_id(cfg.method)
            stack.set_visible_child_name(cfg.method)
            host.set_text(cfg.host)
            port.set_value(cfg.port)
            dev.get_child().set_text(cfg.device)
            queue.get_child().set_text(cfg.cups_queue)
            fentry.set_text(cfg.file_path)
            default.set_active(cfg is state.default)
            default.set_sensitive(cfg is not state.default)   # always keep one default
            result.set_text("")
            state.loading = False

        def update_row(row):
            row.label.set_text(row.printer.name or "Unnamed printer")
            row.tag.set_visible(row.printer is state.default)

        def fill_list(select):
            for row in listbox.get_children():
                listbox.remove(row)
            for cfg in printers:
                row = Gtk.ListBoxRow()
                row.printer = cfg
                hbox = Gtk.Box(spacing=8, margin=6)
                hbox.pack_start(Gtk.Image.new_from_icon_name(PRINTER_ICONS[cfg.method],
                                                             Gtk.IconSize.BUTTON), False, False, 0)
                row.label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                hbox.pack_start(row.label, True, True, 0)
                row.tag = dim_label("Default", wrap=False)
                row.tag.set_no_show_all(True)
                hbox.pack_end(row.tag, False, False, 0)
                update_row(row)
                row.add(hbox)
                listbox.add(row)
                if cfg is select:
                    listbox.select_row(row)
            listbox.show_all()
            remove.set_sensitive(len(printers) > 1)

        def selected(_lb, row):
            if row is not None and row.printer is not state.current:
                store()
                load(row.printer)

        def changed(*_):
            store()
            row = listbox.get_selected_row()
            if row is not None and not state.loading:
                update_row(row)

        def method_changed(c):
            stack.set_visible_child_name(c.get_active_id())
            if not state.loading:
                store()
                fill_list(state.current)

        def add():
            store()
            cfg = printing.PrinterConfig(name=f"Printer {len(printers) + 1}", host="")
            printers.append(cfg)
            fill_list(cfg)
            name.grab_focus()

        def delete():
            cfg = state.current
            if cfg is None or len(printers) < 2:
                return
            i = next(i for i, p in enumerate(printers) if p is cfg)
            del printers[i]
            if state.default is cfg:
                state.default = printers[0]
            state.current = None
            fill_list(printers[min(i, len(printers) - 1)])

        def default_toggled(b):
            if state.loading or not b.get_active():
                return
            state.default = state.current
            b.set_sensitive(False)
            for row in listbox.get_children():
                update_row(row)

        def run_test():
            store()
            result.set_text(check_printer(state.current))

        addb = icon_button("list-add-symbolic", "Add printer", add, flat=False)
        remove = icon_button("list-remove-symbolic", "Remove printer", delete, flat=False)
        tools.pack_start(addb, False, False, 0)
        tools.pack_start(remove, False, False, 0)
        listbox.connect("row-selected", selected)
        for w in (name, host, fentry, dev.get_child(), queue.get_child()):
            w.connect("changed", changed)
        port.connect("value-changed", changed)
        method.connect("changed", method_changed)
        default.connect("toggled", default_toggled)
        test.connect("clicked", lambda *_: run_test())

        dlg.set_default_size(760, -1)
        dlg.show_all()
        fill_list(in_use)
        if dlg.run() == Gtk.ResponseType.OK:
            store()
            index = next(i for i, p in enumerate(printers) if p is state.default)
            printing.store_printers(self.settings, printers, index)
            save_settings(self.settings)
            self.printers, self.default_printer = printers, index
            self.printer = in_use if any(p is in_use for p in printers) else state.default
            self.notify(f"{len(printers)} printer{'s' if len(printers) != 1 else ''} saved · "
                        f"default: {state.default.name}")
        dlg.destroy()

    # ---- preferences --------------------------------------------------------

    def preferences_dialog(self):
        dlg = Gtk.Dialog(title="Preferences", transient_for=self, modal=True, use_header_bar=True)
        dlg.set_default_size(440, -1)
        prefs = SimpleNamespace(units=self.unit,
                                show_grid=self.canvas.show_grid,
                                grid_mm=float(self.canvas.grid_mm),
                                snap=self.canvas.snap,
                                check_updates=self.settings.get("check_updates", True))

        def changed(attr):
            v = getattr(prefs, attr)
            if attr == "units":
                self.unit = v
                self.settings["units"] = v
                self.props_form = None
                self.build_props()
                self.refresh_label_panel()
                self.refresh_layers()
                self.update_status()
            elif attr in ("show_grid", "snap"):
                action = "show-grid" if attr == "show_grid" else "snap"
                self.lookup_action(action).set_state(GLib.Variant.new_boolean(v))
                self.settings[attr] = v
                (self._set_grid if attr == "show_grid" else self._set_snap)(v)
            elif attr == "grid_mm":
                self.settings["grid_mm"] = v
                self.canvas.grid_mm = v
                self.canvas.queue_draw()
            elif attr == "check_updates":
                self.settings["check_updates"] = v
            save_settings(self.settings)

        form = PropertyForm(changed)
        form.section("Editing")
        form.combo("Units", prefs, "units", [("mm", "Millimetres"), ("in", "Inches"),
                                             ("dots", "Printer dots")])
        form.switch("Show grid", prefs, "show_grid")
        if self.unit == "in":
            spacings = [(1.27, "0.05 in"), (2.54, "0.1 in"), (3.175, "⅛ in"), (6.35, "¼ in"),
                        (12.7, "½ in")]
        else:
            spacings = [(1.0, "1 mm"), (2.0, "2 mm"), (2.5, "2.5 mm"), (5.0, "5 mm"),
                        (10.0, "10 mm")]
        if all(abs(v - prefs.grid_mm) > 1e-6 for v, _t in spacings):
            spacings.append((prefs.grid_mm, f"{prefs.grid_mm:g} mm"))
        form.combo("Grid spacing", prefs, "grid_mm", spacings)
        form.switch("Snap to edges and centers", prefs, "snap")
        form.hint("With the grid shown, snapping also lines things up with it. Hold Alt "
                  "while dragging to place things freely.")
        form.section("Updates")
        if FLATPAK:
            form.hint(f"You have version {__version__}. Updates come through Flatpak, "
                      "with your software centre or flatpak update.")
        else:
            form.switch("Check for updates automatically", prefs, "check_updates")
            row = Gtk.Box(spacing=10)
            btn = Gtk.Button(label="Check Now")
            btn.connect("clicked", lambda *_: self.check_updates(manual=True))
            row.pack_start(btn, False, False, 0)
            row.pack_start(dim_label(f"You have version {__version__}."), True, True, 0)
            form.widget(row)
        dlg.get_content_area().add(form)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def shortcuts_window(self):
        ui = SHORTCUTS_UI.format(
            files=_shortcut_rows([("<Primary>n", "New label in a new tab"), ("<Primary>o", "Open"),
                                  ("<Primary>s", "Save"), ("<Primary><Shift>s", "Save as"),
                                  ("<Primary>e", "Export ZPL"), ("<Primary>p", "Print"),
                                  ("<Primary>w", "Close tab"),
                                  ("<Primary>Page_Down <Primary>Page_Up", "Next / previous tab")]),
            editing=_shortcut_rows([("<Primary>z", "Undo"), ("<Primary><Shift>z", "Redo"),
                                    ("<Primary>d", "Duplicate"), ("Delete", "Delete"),
                                    ("<Primary>r", "Rotate 90° clockwise"),
                                    ("<Primary>a", "Select everything"),
                                    ("<Primary>l", "Lock or unlock the selection"),
                                    ("Tab", "Select next element"),
                                    ("Return", "Edit the selected element's content"),
                                    ("Left Right Up Down", "Move by one dot"),
                                    ("<Shift>Left <Shift>Right", "Move by one millimetre"),
                                    ("Escape", "Deselect")]),
            view=_shortcut_rows([("<Primary>plus", "Zoom in"), ("<Primary>minus", "Zoom out"),
                                 ("<Primary>0", "Actual size"), ("<Primary>9", "Fit to window"),
                                 ("<Primary>apostrophe", "Show grid"),
                                 ("<Primary>comma", "Preferences")]))
        w = Gtk.Builder.new_from_string(ui, -1).get_object("shortcuts")
        w.set_transient_for(self)
        w.show_all()

    # ---- updates ------------------------------------------------------------

    def check_updates(self, manual: bool):
        if manual:
            self.notify("Checking for updates…")

        def work():
            try:
                rel, err = updater.fetch_latest(), None
            except (OSError, ValueError) as e:
                rel, err = None, str(getattr(e, "reason", None) or e)
            GLib.idle_add(self._on_update_checked, rel, err, manual)

        threading.Thread(target=work, daemon=True).start()
        return True

    def _on_update_checked(self, rel, err, manual):
        if err:
            if manual:
                self.notify(f"Couldn't check for updates: {err}")
            return False
        self.settings["last_update_check"] = time.time()
        save_settings(self.settings)
        if not updater.is_newer(rel.version):
            self.latest = None
            if manual:
                self.notify(f"You're up to date ({APP_NAME} {__version__}).")
            return False
        self.latest = rel
        method = updater.install_method()
        can_install = bool(updater.asset_for(rel, method)) and bool(shutil.which("pkexec"))
        self.update_install_btn.set_label("Install" if can_install else "Download")
        self.update_text.set_markup(f"<b>{APP_NAME} {GLib.markup_escape_text(rel.version)}</b> "
                                    f"is available. You have {__version__}.")
        self.update_restart_btn.hide()
        self.update_install_btn.show()
        self.update_notes_btn.show()
        self.update_bar.show()
        return False

    def install_update(self):
        rel = self.latest
        if not rel or self.updating:
            return
        method = updater.install_method()
        asset = updater.asset_for(rel, method)
        if not asset or not shutil.which("pkexec"):
            hint = {
                "package": f"{APP_NAME} was installed by your package manager; update it there "
                           "(for example with 'yay -Syu').",
                "manual": "This copy runs from source, so it can't replace itself. "
                          "Opening the download page.",
            }.get(method, "Opening the download page.")
            self.notify(hint)
            Gtk.show_uri_on_window(self, rel.url, Gdk.CURRENT_TIME)
            return
        if not self.ask(f"Install {APP_NAME} {rel.version}?",
                        f"{asset} will be downloaded from GitHub, checked against its published "
                        "SHA-256 checksum, and installed. You'll be asked for your password.",
                        "Install"):
            return
        self.updating = True
        self.update_install_btn.set_sensitive(False)
        self.update_text.set_text(f"Downloading {asset}…")

        def progress(frac):
            GLib.idle_add(self.update_text.set_text, f"Downloading {asset}… {int(frac * 100)}%")

        def work():
            tmp = tempfile.mkdtemp(prefix="zplinux-update-")
            try:
                path = updater.download(rel, asset, tmp, progress)
                cmd = updater.install_command(method, path, tmp)
                GLib.idle_add(self.update_text.set_text, "Installing…")
                proc = subprocess.run(cmd, capture_output=True, text=True)
                out, rc = (proc.stdout + proc.stderr).strip(), proc.returncode
            except (OSError, ValueError) as e:
                out, rc = str(e), -1
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            GLib.idle_add(self._on_update_installed, rel, rc, out)

        threading.Thread(target=work, daemon=True).start()

    def _on_update_installed(self, rel, rc, out):
        self.updating = False
        self.update_install_btn.set_sensitive(True)
        if rc == 0:
            self.latest = None
            self.update_text.set_markup(f"<b>{APP_NAME} {GLib.markup_escape_text(rel.version)}</b> "
                                        "is installed. Restart to start using it.")
            self.update_install_btn.hide()
            self.update_notes_btn.hide()
            self.update_restart_btn.show()
        elif rc in (126, 127):
            self.update_text.set_text("Update cancelled: authorization was denied.")
        else:
            self.update_text.set_text("The update failed.")
            self.error("The update failed", (out.splitlines() or ["Unknown error"])[-1])
        return False

    def restart(self):
        if self.on_delete_event():
            return
        argv = [sys.argv[0]] + [d.path for d in self.docs if d.path]
        os.execv(sys.executable, [sys.executable] + argv)

    # ---- dialogs ------------------------------------------------------------

    def error(self, title, detail):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE, text=title)
        dlg.format_secondary_text(detail)
        dlg.run()
        dlg.destroy()

    def ask(self, title, detail, ok="OK") -> bool:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                text=title)
        dlg.format_secondary_text(detail)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, ok, Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        r = dlg.run()
        dlg.destroy()
        return r == Gtk.ResponseType.OK

    def about(self):
        dlg = Gtk.AboutDialog(transient_for=self, modal=True, program_name=APP_NAME,
                              version=__version__, logo_icon_name="zplinux",
                              comments="Design and print labels for Zebra and other ZPL "
                                       "thermal printers.",
                              website=f"https://github.com/{updater.REPO}",
                              website_label="Project page",
                              license_type=Gtk.License.GPL_3_0_ONLY)
        dlg.run()
        dlg.destroy()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # ZPLinux's own icons: from the source tree, or where install.sh puts them.
        theme = Gtk.IconTheme.get_default()
        for d in (os.path.join(HERE, "data", "icons"), os.path.join(HERE, "data"),
                  os.path.join(os.path.dirname(os.path.dirname(HERE)), "share", "zplinux", "icons")):
            if os.path.isdir(d):
                theme.append_search_path(d)

    def do_activate(self):
        if not self.window:
            self.window = Window(self)
        self.window.present()

    def do_open(self, files, n, hint):
        paths = [f.get_path() for f in files if f.get_path()]
        if not self.window:
            self.window = Window(self, paths[0] if paths else None)
            paths = paths[1:]
        for path in paths:
            self.window.open_path(path)
        self.window.present()


def main() -> int:
    return App().run(sys.argv)
