"""Small GTK building blocks: property cards bound to object attributes, and toasts."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from .model import UNITS, from_unit, to_unit  # noqa: E402

CSS = b"""
headerbar.accent-bar, headerbar.accent-bar:backdrop {
    background-image: linear-gradient(to bottom, #1f6fc5, #165aa7);
    background-color: #165aa7;
    border-color: #0f4a8c;
    box-shadow: inset 0 -1px rgba(0, 0, 0, 0.15);
    color: #fff;
}
headerbar.accent-bar:backdrop { background-image: none; background-color: #1d63b3; }
headerbar.accent-bar .title, headerbar.accent-bar .subtitle,
headerbar.accent-bar label, headerbar.accent-bar image { color: #fff; }
headerbar.accent-bar button {
    background-image: none;
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
    color: #fff;
    text-shadow: none;
    -gtk-icon-shadow: none;
}
headerbar.accent-bar button:hover { background-color: rgba(255, 255, 255, 0.16); }
headerbar.accent-bar button:active, headerbar.accent-bar button:checked {
    background-color: rgba(0, 0, 0, 0.18);
}
headerbar.accent-bar button:disabled, headerbar.accent-bar button:disabled image { color: rgba(255, 255, 255, 0.45); }
headerbar.accent-bar button.suggested-action {
    background-color: #fff;
    border-color: #fff;
}
headerbar.accent-bar button.suggested-action label,
headerbar.accent-bar button.suggested-action image { color: #165aa7; font-weight: bold; }
headerbar.accent-bar button.suggested-action:hover { background-color: #e6effa; }
headerbar.accent-bar button.suggested-action:disabled { background-color: rgba(255, 255, 255, 0.5); }
.sidebar { background-color: @theme_base_color; }
.sidebar-title { font-weight: bold; font-size: 0.85em; opacity: 0.6; margin: 10px 4px 4px 4px; }
.card {
    background-color: @theme_base_color;
    border: 1px solid alpha(@theme_fg_color, 0.12);
    border-radius: 10px;
}
.card-title { font-weight: bold; font-size: 0.85em; opacity: 0.6; margin: 4px 2px 0 2px; }
.tile {
    border-radius: 10px;
    padding: 8px 2px 6px 2px;
    background-image: none;
    background-color: alpha(@theme_fg_color, 0.05);
    border: 1px solid alpha(@theme_fg_color, 0.08);
    box-shadow: none;
}
.tile:hover { background-color: alpha(@theme_selected_bg_color, 0.15); }
.tile label { font-size: 0.85em; }
.canvas-bg, .canvas-bg viewport { background-color: shade(@theme_bg_color, 0.86); }
.floating-bar {
    background-color: @theme_base_color;
    border: 1px solid alpha(@theme_fg_color, 0.15);
    border-radius: 999px;
    padding: 2px 6px;
    margin: 12px;
}
.floating-bar button { border-radius: 999px; min-height: 24px; min-width: 24px; padding: 2px; }
.status-pill {
    background-color: alpha(@theme_base_color, 0.92);
    border-radius: 999px;
    padding: 4px 12px;
    margin: 12px;
    font-size: 0.85em;
}
.toast {
    background-color: rgba(28, 28, 30, 0.94);
    color: #fff;
    border-radius: 999px;
    padding: 4px 6px 4px 18px;
    margin: 20px 20px 64px 20px;
}
.toast button { color: #fff; border-radius: 999px; background: none; border: none; box-shadow: none; }
.toast button:hover { background-color: rgba(255, 255, 255, 0.15); }
.layers row { border-radius: 8px; margin: 1px 4px; padding: 4px 6px; }
.layer-kind { font-size: 0.8em; opacity: 0.6; }
.welcome-title { font-size: 28px; font-weight: 800; }
.welcome-sub { opacity: 0.7; }
.template-card {
    border-radius: 12px;
    padding: 10px;
    background-image: none;
    background-color: @theme_base_color;
    border: 1px solid alpha(@theme_fg_color, 0.12);
    box-shadow: none;
}
.template-card:hover { border-color: @theme_selected_bg_color; }
.template-name { font-weight: bold; }
.dim { opacity: 0.6; }
.zpl-view { font-family: monospace; font-size: 0.9em; padding: 8px; }
.empty-state image { opacity: 0.35; }
"""


def dim_label(text: str, wrap: bool = True, xalign: float = 0) -> Gtk.Label:
    lab = Gtk.Label(label=text, xalign=xalign, wrap=wrap, max_width_chars=34)
    lab.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lab.get_style_context().add_class("dim")
    return lab


def icon_button(icon: str, tip: str, cb: Callable[[], None], flat: bool = True) -> Gtk.Button:
    b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    b.set_tooltip_text(tip)
    if flat:
        b.set_relief(Gtk.ReliefStyle.NONE)
    b.connect("clicked", lambda *_: cb())
    return b


class PropertyForm(Gtk.Box):
    """A column of cards with labelled inputs bound to object attributes.

    Lengths are stored in dots and shown in the chosen unit.
    """

    def __init__(self, on_change: Callable[[str], None], unit: str = "mm", dpi: int = 203):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        self.on_change = on_change
        self.unit = unit
        self.dpi = dpi
        self.grid: Optional[Gtk.Grid] = None
        self.row = 0
        self.spins = {}
        self.updating = False
        self.first_input: Optional[Gtk.Widget] = None

    def section(self, title: Optional[str] = None) -> None:
        if title:
            lab = Gtk.Label(label=title, xalign=0)
            lab.get_style_context().add_class("card-title")
            self.pack_start(lab, False, False, 0)
        self.grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=10)
        card = Gtk.Box()
        card.get_style_context().add_class("card")
        card.pack_start(self.grid, True, True, 0)
        self.pack_start(card, False, False, 0)
        self.row = 0

    def _attach(self, text: Optional[str], widget: Gtk.Widget) -> None:
        if self.grid is None:
            self.section()
        if text is None:
            widget.set_hexpand(True)
            self.grid.attach(widget, 0, self.row, 2, 1)
        else:
            lab = Gtk.Label(label=text, xalign=0)
            self.grid.attach(lab, 0, self.row, 1, 1)
            widget.set_hexpand(True)
            self.grid.attach(widget, 1, self.row, 1, 1)
        self.row += 1

    def _input(self, w: Gtk.Widget) -> None:
        if self.first_input is None:
            self.first_input = w

    def _spin(self, text, obj, attr, lo, hi, step, digits, scale_in, scale_out):
        adj = Gtk.Adjustment(value=scale_in(getattr(obj, attr)), lower=lo, upper=hi,
                             step_increment=step, page_increment=step * 10)
        sb = Gtk.SpinButton(adjustment=adj, digits=digits, numeric=True, width_chars=7)

        def changed(w):
            if self.updating:
                return
            setattr(obj, attr, scale_out(w.get_value()))
            self.on_change(attr)
        sb.connect("value-changed", changed)
        self.spins[attr] = (sb, obj, scale_in)
        self._attach(text, sb)
        return sb

    def spin(self, text, obj, attr, lo, hi, step=1, digits=0):
        """A plain number (counts, levels, sizes that are always in dots)."""
        conv = (lambda v: v) if digits else (lambda v: int(round(v)))
        return self._spin(text, obj, attr, lo, hi, step, digits, lambda v: v, conv)

    def length(self, text, obj, attr, lo_dots, hi_dots):
        """A length stored in dots, shown in the current unit."""
        label, digits = UNITS[self.unit]
        step = {"mm": 0.5, "in": 0.01, "dots": 1}[self.unit]
        return self._spin(f"{text} ({label})", obj, attr,
                          to_unit(lo_dots, self.unit, self.dpi), to_unit(hi_dots, self.unit, self.dpi),
                          step, digits,
                          lambda v: to_unit(v, self.unit, self.dpi),
                          lambda v: max(lo_dots, from_unit(v, self.unit, self.dpi)))

    def entry(self, text, obj, attr, placeholder=""):
        e = Gtk.Entry(text=getattr(obj, attr), placeholder_text=placeholder)

        def changed(w):
            setattr(obj, attr, w.get_text())
            self.on_change(attr)
        e.connect("changed", changed)
        self._input(e)
        self._attach(text, e)
        return e

    def textarea(self, text, obj, attr):
        tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                          left_margin=6, right_margin=6)
        tv.get_buffer().set_text(getattr(obj, attr))
        frame = Gtk.ScrolledWindow(min_content_height=70, shadow_type=Gtk.ShadowType.IN)
        frame.add(tv)

        def changed(buf):
            setattr(obj, attr, buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
            self.on_change(attr)
        tv.get_buffer().connect("changed", changed)
        self._input(tv)
        if text:
            lab = Gtk.Label(label=text, xalign=0)
            self._attach(None, lab)
        self._attach(None, frame)
        return tv

    def switch(self, text, obj, attr):
        sw = Gtk.Switch(active=getattr(obj, attr), halign=Gtk.Align.END, valign=Gtk.Align.CENTER)

        def toggled(w, _p):
            setattr(obj, attr, w.get_active())
            self.on_change(attr)
        sw.connect("notify::active", toggled)
        self._attach(text, sw)
        return sw

    def combo(self, text, obj, attr, options):
        """options: list of (value, label)."""
        c = Gtk.ComboBoxText()
        for v, lab in options:
            c.append(str(v), lab)
        c.set_active_id(str(getattr(obj, attr)))

        def changed(w):
            cur = getattr(obj, attr)
            setattr(obj, attr, type(cur)(w.get_active_id()))
            self.on_change(attr)
        c.connect("changed", changed)
        self._attach(text, c)
        return c

    def toggle_group(self, text, obj, attr, options):
        """A linked row of toggle buttons: options are (value, icon, tooltip)."""
        box = Gtk.Box()
        box.get_style_context().add_class("linked")
        buttons = []

        def clicked(btn, value):
            if self.updating or not btn.get_active():
                return
            self.updating = True
            for b, v in buttons:
                b.set_active(v == value)
            self.updating = False
            setattr(obj, attr, value)
            self.on_change(attr)
        for value, icon, tip in options:
            b = Gtk.ToggleButton()
            b.add(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            b.set_tooltip_text(tip)
            b.set_active(getattr(obj, attr) == value)
            b.connect("toggled", clicked, value)
            buttons.append((b, value))
            box.pack_start(b, True, True, 0)
        self._attach(text, box)
        return box

    def hint(self, text):
        self._attach(None, dim_label(text))

    def widget(self, w: Gtk.Widget, text: Optional[str] = None):
        self._attach(text, w)

    def refresh_spins(self, *attrs):
        self.updating = True
        for a in attrs:
            if a in self.spins:
                sb, obj, scale_in = self.spins[a]
                sb.set_value(scale_in(getattr(obj, a)))
        self.updating = False


class Toast(Gtk.Revealer):
    """A transient message at the bottom of the window, with an optional action."""

    def __init__(self):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
        box = Gtk.Box(spacing=6)
        box.get_style_context().add_class("toast")
        self.label = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        self.action = Gtk.Button()
        self.action.set_no_show_all(True)
        self.action.connect("clicked", self._on_action)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close.connect("clicked", lambda *_: self.dismiss())
        box.pack_start(self.label, True, True, 0)
        box.pack_start(self.action, False, False, 0)
        box.pack_start(close, False, False, 0)
        self.add(box)
        self._cb: Optional[Callable[[], None]] = None
        self._timer = 0

    def show_message(self, text: str, action: Optional[str] = None,
                     callback: Optional[Callable[[], None]] = None, seconds: int = 4):
        self.label.set_text(text)
        self._cb = callback
        if action:
            self.action.set_label(action)
            self.action.show()
        else:
            self.action.hide()
        self.set_reveal_child(True)
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add_seconds(seconds, self._expire)

    def _expire(self):
        self._timer = 0
        self.set_reveal_child(False)
        return False

    def dismiss(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.set_reveal_child(False)

    def _on_action(self, *_):
        cb = self._cb
        self.dismiss()
        if cb:
            cb()
