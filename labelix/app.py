"""Labelix GTK3 user interface: a WYSIWYG designer for ZPL labels."""

from __future__ import annotations

import base64
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Callable, List, Optional
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, __version__, printing, render, updater, zpl  # noqa: E402
from .canvas import LabelCanvas  # noqa: E402
from .model import (DPI_CHOICES, KIND_NAMES, MM_PER_INCH, UNITS, BoxElement,  # noqa: E402
                    Code128Element, EllipseElement, Element, ImageElement, Label, QRElement,
                    TextElement, describe, dpmm, element_from_dict, to_unit)
from .templates import TEMPLATES  # noqa: E402
from .widgets import CSS, PropertyForm, Toast, dim_label, icon_button  # noqa: E402

SETTINGS_FILE = os.path.join(GLib.get_user_config_dir(), "labelix", "settings.json")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLED = HERE.startswith("/usr/")
# How to start the bundled fake printer: an installed command, or the script in the source tree.
FAKE_PRINTER_CMD = ("labelix-fake-printer --render" if INSTALLED
                    else "python3 tools/fake_printer.py --render")
MAX_RECENT = 8

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
    "text": "labelix-text-symbolic",
    "code128": "labelix-barcode-symbolic",
    "qr": "labelix-qr-symbolic",
    "box": "labelix-box-symbolic",
    "ellipse": "labelix-ellipse-symbolic",
    "image": "labelix-image-symbolic",
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


def file_paths(data) -> List[str]:
    return [unquote(urlparse(u).path) for u in (data.get_uris() or [])
            if urlparse(u).scheme == "file"]


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, path: Optional[str] = None):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1320, 860)
        self.set_icon_name("labelix")
        self.settings = load_settings()
        self.printer = printing.PrinterConfig.from_dict(self.settings.get("printer", {}))
        self.unit = self.settings.get("units", "mm")
        self.label = Label()
        self.path: Optional[str] = None
        self.dirty = False
        self.selected: Optional[Element] = None
        self.values: dict = {}           # preview/print values for {variables}
        self.undo_stack: List[str] = []
        self.redo_stack: List[str] = []
        self.undo_key: Optional[str] = None
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
        if path:
            self.open_path(path)
        else:
            self.show_welcome()

        if self.settings.get("check_updates", True) and \
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

        add("new", self.new_label, ["<Primary>n"])
        add("open", self.open_dialog, ["<Primary>o"])
        add("save", self.save, ["<Primary>s"])
        add("save-as", self.save_as, ["<Primary><Shift>s"])
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

    EDITOR_ACTIONS = ("save", "save-as", "export-zpl", "export-png", "print", "print-csv",
                      "undo", "redo", "duplicate", "zoom-in", "zoom-out", "zoom-100", "zoom-fit")

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

        menu = Gio.Menu()
        for items in ((("Save As…", "win.save-as"), ("Export ZPL…", "win.export-zpl"),
                       ("Export PNG Preview…", "win.export-png")),
                      (("Print from CSV…", "win.print-csv"),
                       ("Printer Settings…", "win.printer-settings")),
                      (("Show Grid", "win.show-grid"), ("Snap to Guides", "win.snap")),
                      (("Preferences", "win.preferences"),
                       ("Keyboard Shortcuts", "win.shortcuts"),
                       ("Check for Updates", "win.check-updates"),
                       (f"About {APP_NAME}", "win.about"))):
            sec = Gio.Menu()
            for text, action in items:
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
        box.set_size_request(280, -1)
        pop.add(box)
        t = Gtk.Label(label="Printer", xalign=0)
        t.get_style_context().add_class("card-title")
        box.pack_start(t, False, False, 0)
        row = Gtk.Box(spacing=10)
        self.pp_icon = Gtk.Image()
        row.pack_start(self.pp_icon, False, False, 0)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pp_method = Gtk.Label(xalign=0)
        self.pp_target = dim_label("")
        col.pack_start(self.pp_method, False, False, 0)
        col.pack_start(self.pp_target, False, False, 0)
        row.pack_start(col, True, True, 0)
        box.pack_start(row, False, False, 0)
        self.pp_result = dim_label("")
        self.pp_result.set_selectable(True)
        self.pp_result.set_no_show_all(True)
        box.pack_start(self.pp_result, False, False, 0)
        btns = Gtk.Box(spacing=6, homogeneous=True)
        change = Gtk.Button(label="Change…")
        change.connect("clicked", lambda *_: (pop.popdown(), self.printer_dialog()))
        test = Gtk.Button(label="Test Connection")
        test.connect("clicked", lambda *_: self._test_printer())
        btns.pack_start(change, True, True, 0)
        btns.pack_start(test, True, True, 0)
        box.pack_start(btns, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 2)
        csvb = Gtk.ModelButton(text="Print a Batch from CSV…")
        csvb.set_action_name("win.print-csv")
        box.pack_start(csvb, False, False, 0)
        box.show_all()
        pop.connect("show", lambda *_: self._update_printer_info())
        return pop

    def _update_printer_info(self):
        icon = {"network": "network-wired-symbolic", "device": "drive-harddisk-usb-symbolic",
                "cups": "printer-symbolic", "file": "text-x-generic-symbolic"}[self.printer.method]
        self.pp_icon.set_from_icon_name(icon, Gtk.IconSize.DND)
        self.pp_method.set_text(printing.METHOD_LABELS[self.printer.method])
        self.pp_target.set_text(self.printer.describe())
        self.pp_result.hide()

    def _test_printer(self):
        try:
            msg = "✓ " + printing.test(self.printer)
        except printing.PrintError as e:
            msg = "✗ " + str(e)
            if self.printer.method == "network" and self.printer.host in ("127.0.0.1", "localhost"):
                msg += f"\nNo printer yet? Run: {FAKE_PRINTER_CMD}"
        self.pp_result.set_text(msg)
        self.pp_result.show()

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
        logo = Gtk.Image.new_from_icon_name("labelix", Gtk.IconSize.DIALOG)
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

    def _template_card(self, name, size, factory) -> Gtk.Widget:
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
        return self._card(area, name, size, lambda: self.start_from(factory))

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
        self.stack.set_visible_child_name("welcome")
        for w in (self.hist_box, self.print_box, self.save_btn):
            w.hide()
        self._editor_actions_enabled(False)
        self.header.set_subtitle(None)
        self.path = None
        self.dirty = False

    def show_editor(self):
        self.stack.set_visible_child_name("editor")
        for w in (self.hist_box, self.print_box, self.save_btn):
            w.show()
        self._editor_actions_enabled(True)
        self.update_title()
        GLib.idle_add(self.zoom_fit)

    # ---- editor layout ------------------------------------------------------

    def _build_editor(self) -> Gtk.Widget:
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

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
        self.layers = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.layers.get_style_context().add_class("layers")
        self.layers.connect("row-selected", self._on_layer_selected)
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
        return main

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
        if self.confirm_discard():
            self.show_welcome()

    def start_from(self, factory: Callable) -> bool:
        if self.in_editor() and not self.confirm_discard():
            return False
        self.label, self.values = factory()
        self.path = None
        self._load_document()
        return True

    def _load_document(self):
        self._reset_history()
        self.selected = None
        self.props_form = None
        self._layers_sig = None
        self.dirty = False
        self.refresh_label_panel()
        self.build_props()
        self.canvas.resize_to_label()
        self.refresh_zpl()
        self.refresh_layers()
        self.update_status()
        self.show_editor()

    def open_dialog(self):
        if not self.confirm_discard():
            return
        dlg = Gtk.FileChooserNative.new("Open Label", self, Gtk.FileChooserAction.OPEN, None, None)
        f = Gtk.FileFilter()
        f.set_name("Labelix labels (*.labelix)")
        f.add_pattern("*.labelix")
        f.add_pattern("*.json")
        dlg.add_filter(f)
        self._set_dialog_folder(dlg)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            self.open_path(dlg.get_filename())

    def open_path(self, path):
        try:
            with open(path) as f:
                text = f.read()
            label = Label.from_json(text)
            values = json.loads(text).get("preview_values", {})
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.error("Could not open label", str(e))
            return
        self.label, self.values, self.path = label, values, path
        self._remember_folder(path)
        self._add_recent(path)
        self._load_document()

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
        path = self.choose_save("Save Label", "label.labelix", ("Labelix labels", "*.labelix"))
        if not path:
            return False
        if not path.endswith((".labelix", ".json")):
            path += ".labelix"
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
        base = os.path.splitext(os.path.basename(self.path))[0] if self.path else None
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
        return not self.confirm_discard()

    def drop_files(self, paths, x=None, y=None):
        """Files dropped on the window or canvas: open labels, add images."""
        for p in paths:
            if p.lower().endswith((".labelix", ".json")):
                if self.confirm_discard():
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
            self.notify("Only images and .labelix files can be dropped here.")

    # ---- history ------------------------------------------------------------

    def _reset_history(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.undo_key = None
        self._update_history_buttons()

    def _snapshot(self) -> str:
        idx = self.label.elements.index(self.selected) if self.selected in self.label.elements else -1
        return json.dumps({"label": self.label.to_json(), "sel": idx, "values": self.values})

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
        el = self.selected
        if not el:
            return
        self.checkpoint()
        self.label.elements.remove(el)
        self.select(None)
        self.structure_changed()
        self.notify(f"{KIND_NAMES[el.kind]} deleted", "Undo", self.undo)

    def duplicate_selected(self):
        if not self.selected:
            return
        self.checkpoint()
        el = element_from_dict(self.selected.to_dict())
        el.x += dpmm(self.label.dpi) * 2
        el.y += dpmm(self.label.dpi) * 2
        self.label.elements.append(el)
        self.select(el)
        self.structure_changed()

    def restack(self, where: str):
        el = self.selected
        if not el:
            return
        els = self.label.elements
        i = els.index(el)
        j = {"top": len(els) - 1, "bottom": 0, "up": min(len(els) - 1, i + 1),
             "down": max(0, i - 1)}[where]
        if i == j:
            return
        self.checkpoint()
        els.insert(j, els.pop(i))
        self.structure_changed()

    def align(self, where: str):
        el = self.selected
        if not el:
            return
        _x, _y, w, h = el.bounds()
        W, H = self.label.width_dots, self.label.height_dots
        self.checkpoint()
        if where == "left":
            el.x = 0
        elif where == "hcenter":
            el.x = max(0, round((W - w) / 2))
        elif where == "right":
            el.x = max(0, W - w)
        elif where == "top":
            el.y = 0
        elif where == "vcenter":
            el.y = max(0, round((H - h) / 2))
        elif where == "bottom":
            el.y = max(0, H - h)
        self.element_changed(el)

    def select(self, el: Optional[Element]):
        if el is self.selected and (el is None or self.props_form is not None):
            return
        self.selected = el
        self.undo_key = None
        self.build_props()
        if el is not None:
            self.panel_stack.set_visible_child_name("element")
        self._sync_layer_selection()
        self.canvas.queue_draw()
        self.update_status()

    def activate_element(self, el: Element):
        """Double-click or Enter: jump to the element's main field."""
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
        sig = tuple((id(e), e.kind, self.describe(e)) for e in els) + (self.unit,)
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
                row.add(box)
                row.show_all()
                self.layers.add(row)
            self._syncing_layers = False
        self._sync_layer_selection()

    def _sync_layer_selection(self):
        self._syncing_layers = True
        target = next((r for r in self.layers.get_children() if r.element is self.selected), None)
        if target is not None:
            self.layers.select_row(target)
        else:
            self.layers.unselect_all()
        self._syncing_layers = False

    def _on_layer_selected(self, lb, row):
        if not self._syncing_layers:
            self.select(row.element if row is not None else None)

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
            form.hint("Type {name} to fill in a value at print time.")
            form.section("Font")
            form.length("Size", p, "height", 10, 2000)
            form.length("Character width", p, "width", 0, 2000)
            form.hint("Character width 0 keeps normal proportions.")
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
            form.section("Barcode data")
            form.entry(None, p, "data", "e.g. ABC-123 or {sku}")
            form.hint("Code 128: letters, digits and symbols (printable ASCII).")
            form.section("Size")
            form.length("Bar height", p, "height", 10, maxh)
            form.spin("Narrow bar (dots)", p, "module", 1, 10)
            form.switch("Show text below", p, "show_text")
        elif isinstance(el, QRElement):
            form.section("QR code data")
            form.entry(None, p, "data", "Text, a link, or {variable}")
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
        form.length("X", p, "x", 0, maxw)
        form.length("Y", p, "y", 0, maxh)
        align = Gtk.Box(spacing=8, homogeneous=True)
        tips = {"left": "Align to left edge", "hcenter": "Center horizontally",
                "right": "Align to right edge", "top": "Align to top edge",
                "vcenter": "Center vertically", "bottom": "Align to bottom edge"}
        for group in (("left", "hcenter", "right"), ("top", "vcenter", "bottom")):
            g = Gtk.Box(homogeneous=True)
            g.get_style_context().add_class("linked")
            for where in group:
                g.pack_start(icon_button(f"labelix-align-{where}-symbolic", tips[where],
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
        delete = icon_button("user-trash-symbolic", "Delete (Del)", self.delete_selected, flat=False)
        delete.get_style_context().add_class("destructive-action")
        arr.pack_end(delete, False, False, 0)
        form.widget(arr)

        self.props_form = form
        self.props_box.add(form)
        self.props_box.show_all()

    # ---- label panel --------------------------------------------------------

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
        self.size_info = dim_label(self._size_text())
        form.widget(self.size_info)

        form.section("Printing")
        form.spin("Copies", p, "copies", 1, 9999)
        form.spin("Darkness adjust", p, "darkness", -30, 30)
        form.hint("0 keeps the printer's own darkness setting.")

        form.section("Variables")
        self.vars_grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        form.widget(self.vars_grid)
        csvb = Gtk.Button(label="Print a Batch from CSV…")
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
        names = self.label.variables()
        if self._shown_vars == names:
            return
        self._shown_vars = names
        for c in grid.get_children():
            grid.remove(c)
        if not names:
            grid.attach(dim_label("Type {name} in any text, barcode or QR field to create a "
                                  "variable. Fill it in here, or print one label per row of a "
                                  "CSV file whose column headers match."), 0, 0, 2, 1)
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
            grid.attach(e, 1, i, 1, 1)
        grid.show_all()

    def _size_text(self):
        lab = self.label
        return (f"{lab.width_dots} × {lab.height_dots} printer dots · "
                f"{lab.width_mm:.1f} × {lab.height_mm:.1f} mm · "
                f"{lab.width_mm / MM_PER_INCH:.2f} × {lab.height_mm / MM_PER_INCH:.2f} in")

    # ---- refresh ------------------------------------------------------------

    def update_title(self):
        name = os.path.basename(self.path) if self.path else "Untitled label"
        self.header.set_subtitle(("• " if self.dirty else "") + name)

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
        if self.selected:
            _bx, _by, bw, bh = self.selected.bounds()
            parts.append(f"{KIND_NAMES[self.selected.kind]} {self.fmt(bw)} × {self.fmt(bh)}")
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
            return False
        key = Gdk.keyval_name(ev.keyval) or ""
        # Leave typing keys alone while an input field has focus; Escape returns to the canvas.
        if isinstance(self.get_focus(), (Gtk.Entry, Gtk.TextView)):
            if key == "Escape":
                self.canvas.grab_focus()
                return True
            return False
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
            self.checkpoint("nudge")
            self.selected.x = max(0, self.selected.x + dx * step)
            self.selected.y = max(0, self.selected.y + dy * step)
            self.element_changed(self.selected)
            return True
        return False

    # ---- printing -----------------------------------------------------------

    def print_label(self):
        missing = [v for v in self.label.variables() if not self.values.get(v)]
        if missing and not self.ask("Some variables are empty",
                                    f"{', '.join(missing)} will print as written. Print anyway?",
                                    "Print"):
            return
        n = self.label.copies
        self._send(self.current_zpl(), f"{n} label{'s' if n != 1 else ''}")

    def print_csv(self):
        dlg = Gtk.FileChooserNative.new("Print from CSV", self, Gtk.FileChooserAction.OPEN, None, None)
        f = Gtk.FileFilter()
        f.set_name("CSV files")
        f.add_pattern("*.csv")
        f.add_pattern("*.CSV")
        dlg.add_filter(f)
        self._set_dialog_folder(dlg)
        if dlg.run() != Gtk.ResponseType.ACCEPT:
            return
        path = dlg.get_filename()
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                except csv.Error:
                    dialect = csv.excel
                rows = list(csv.DictReader(fh, dialect=dialect))
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            self.error("Could not read CSV", str(e))
            return
        if not rows:
            self.error("Nothing to print", "The CSV file has no data rows.")
            return
        needed = self.label.variables()
        cols = [c for c in rows[0].keys() if c]
        missing = [v for v in needed if v not in cols]
        copies = self.label.copies
        detail = (f"{len(rows)} row{'s' if len(rows) != 1 else ''} × {copies} "
                  f"cop{'y' if copies == 1 else 'ies'} to {self.printer.describe()}.\n"
                  f"Columns: {', '.join(cols)}")
        if missing:
            detail += ("\n\nNot in the CSV (the values from the Label tab are used): "
                       + ", ".join(missing))
        if not self.ask(f"Print {len(rows) * copies} labels?", detail, "Print"):
            return
        merged = [{**self.values, **{k: (v or "") for k, v in r.items() if k}} for r in rows]
        self._send(zpl.batch_zpl(self.label, merged), f"{len(rows)} labels from CSV")

    def _send(self, data: str, what: str):
        cfg = self.printer
        if cfg.method == "file" and not cfg.file_path:
            path = self.choose_save("Save Print Job", "job.zpl", ("ZPL files", "*.zpl"))
            if not path:
                return
            cfg = printing.PrinterConfig(method="file", file_path=path)
        try:
            where = printing.send(data, cfg)
        except printing.PrintError as e:
            hint = ""
            if cfg.method == "network" and cfg.host in ("127.0.0.1", "localhost"):
                hint = ("\n\nTesting without a printer? Start the fake printer first:\n"
                        + FAKE_PRINTER_CMD)
            self.error("Printing failed", str(e) + hint)
            return
        except OSError as e:
            self.error("Printing failed", str(e))
            return
        self.notify(f"Sent {what} to {where}")

    def printer_dialog(self):
        dlg = Gtk.Dialog(title="Printer", transient_for=self, modal=True, use_header_bar=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class(
            "suggested-action")
        cfg = printing.PrinterConfig.from_dict(self.printer.to_dict())
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=16)
        dlg.get_content_area().add(grid)

        method = Gtk.ComboBoxText()
        for m in printing.METHODS:
            method.append(m, printing.METHOD_LABELS[m])
        method.set_active_id(cfg.method)
        grid.attach(Gtk.Label(label="Connection", xalign=0), 0, 0, 1, 1)
        grid.attach(method, 1, 0, 1, 1)
        stack = Gtk.Stack(vhomogeneous=False)
        grid.attach(stack, 0, 1, 2, 1)

        g = Gtk.Grid(column_spacing=10, row_spacing=10)
        host = Gtk.Entry(text=cfg.host, hexpand=True, activates_default=True,
                         placeholder_text="e.g. 192.168.1.50")
        port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        port.set_value(cfg.port)
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
        dev.get_child().set_text(cfg.device)
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
        queue.get_child().set_text(cfg.cups_queue)
        queue.set_hexpand(True)
        g.attach(Gtk.Label(label="Printer", xalign=0), 0, 0, 1, 1)
        g.attach(queue, 1, 0, 1, 1)
        g.attach(dim_label("Sends ZPL unchanged with 'lp -o raw'. Set the queue up as a "
                           "Raw printer (no driver) so CUPS does not convert it."), 0, 1, 2, 1)
        stack.add_named(g, "cups")

        g = Gtk.Grid(column_spacing=10, row_spacing=10)
        fentry = Gtk.Entry(text=cfg.file_path, hexpand=True, placeholder_text="Ask each time")
        g.attach(Gtk.Label(label="File", xalign=0), 0, 0, 1, 1)
        g.attach(fentry, 1, 0, 1, 1)
        g.attach(dim_label("Writes print jobs to a .zpl file instead of a printer."), 0, 1, 2, 1)
        stack.add_named(g, "file")

        method.connect("changed", lambda c: stack.set_visible_child_name(c.get_active_id()))
        dlg.set_default_size(480, -1)
        dlg.show_all()
        stack.set_visible_child_name(cfg.method)
        if dlg.run() == Gtk.ResponseType.OK:
            cfg.method = method.get_active_id()
            cfg.host = host.get_text().strip()
            cfg.port = port.get_value_as_int()
            cfg.device = dev.get_child().get_text().strip()
            cfg.cups_queue = queue.get_child().get_text().strip()
            cfg.file_path = fentry.get_text().strip()
            self.printer = cfg
            self.settings["printer"] = cfg.to_dict()
            save_settings(self.settings)
            self.notify(f"Printer set to {printing.METHOD_LABELS[cfg.method]}: {cfg.describe()}")
        dlg.destroy()

    # ---- preferences --------------------------------------------------------

    def preferences_dialog(self):
        dlg = Gtk.Dialog(title="Preferences", transient_for=self, modal=True, use_header_bar=True)
        dlg.set_default_size(440, -1)
        prefs = SimpleNamespace(units=self.unit,
                                show_grid=self.canvas.show_grid,
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
            elif attr == "check_updates":
                self.settings["check_updates"] = v
            save_settings(self.settings)

        form = PropertyForm(changed)
        form.section("Editing")
        form.combo("Units", prefs, "units", [("mm", "Millimetres"), ("in", "Inches"),
                                             ("dots", "Printer dots")])
        form.switch("Show grid", prefs, "show_grid")
        form.switch("Snap to edges and centers", prefs, "snap")
        form.hint("Hold Alt while dragging to place things freely.")
        form.section("Updates")
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
            files=_shortcut_rows([("<Primary>n", "New label"), ("<Primary>o", "Open"),
                                  ("<Primary>s", "Save"), ("<Primary><Shift>s", "Save as"),
                                  ("<Primary>e", "Export ZPL"), ("<Primary>p", "Print")]),
            editing=_shortcut_rows([("<Primary>z", "Undo"), ("<Primary><Shift>z", "Redo"),
                                    ("<Primary>d", "Duplicate"), ("Delete", "Delete"),
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
            tmp = tempfile.mkdtemp(prefix="labelix-update-")
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
        if not self.confirm_discard():
            return
        argv = [sys.argv[0]] + ([self.path] if self.path else [])
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
                              version=__version__, logo_icon_name="labelix",
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
        # Labelix's own icons: from the source tree, or where install.sh puts them.
        theme = Gtk.IconTheme.get_default()
        for d in (os.path.join(HERE, "data", "icons"), os.path.join(HERE, "data"),
                  "/usr/share/labelix/icons"):
            if os.path.isdir(d):
                theme.append_search_path(d)

    def do_activate(self):
        if not self.window:
            self.window = Window(self)
        self.window.present()

    def do_open(self, files, n, hint):
        path = files[0].get_path() if files else None
        if not self.window:
            self.window = Window(self, path)
        elif path and self.window.confirm_discard():
            self.window.open_path(path)
        self.window.present()


def main() -> int:
    return App().run(sys.argv)
