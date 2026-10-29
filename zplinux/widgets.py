"""Small GTK 4 building blocks: property cards bound to object attributes, toasts, and
helpers that keep the GTK 3 era code simple (packing, drop-down lists, modal dialogs
that return an answer, file choosers)."""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .model import UNITS, from_unit, to_unit  # noqa: E402

# The blue bar's own buttons, picked out by where they sit in it: a plain
# "headerbar button" would also reach into the menus that open from the bar.
_BAR = "headerbar.accent-bar"
_BAR_BUTTONS = [f"{_BAR} {path}" for path in (
    ".start > button", ".start > box > button", ".end > button", ".end > box > button",
    ".end > menubutton > button", ".end > box > menubutton > button",
    "windowcontrols > button")]
_PRINT_BUTTONS = [f"{_BAR} .end > box > button.suggested-action",
                  f"{_BAR} .end > box > menubutton.suggested-action > button"]


def _rule(selectors, suffixes, body):
    return ",\n".join(sel + suf for sel in selectors for suf in suffixes) + " {" + body + "}\n"


CSS = (
    f"{_BAR}, {_BAR}:backdrop {{"
    """
    background-image: linear-gradient(to bottom, #1f6fc5, #165aa7);
    background-color: #165aa7;
    border-color: #0f4a8c;
    box-shadow: inset 0 -1px rgba(0, 0, 0, 0.15);
    color: #fff;
}
"""
    f"{_BAR}:backdrop {{ background-image: none; background-color: #1d63b3; }}\n"
    f"{_BAR} .title, {_BAR} .subtitle {{ color: #fff; }}\n"
    + _rule(_BAR_BUTTONS, ("",), """
    background-image: none;
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
    color: #fff;
    text-shadow: none;
    -gtk-icon-shadow: none;
""")
    + _rule(_BAR_BUTTONS, (" label", " image"), " color: #fff; ")
    + _rule(_BAR_BUTTONS, (":hover",), " background-color: rgba(255, 255, 255, 0.16); ")
    + _rule(_BAR_BUTTONS, (":active", ":checked"), " background-color: rgba(0, 0, 0, 0.18); ")
    + _rule(_BAR_BUTTONS, (":disabled", ":disabled label", ":disabled image"),
            " color: rgba(255, 255, 255, 0.45); ")
    + _rule(_PRINT_BUTTONS, ("",), " background-color: #fff; border-color: #fff; ")
    + _rule(_PRINT_BUTTONS, (" label", " image"), " color: #165aa7; font-weight: bold; ")
    + _rule(_PRINT_BUTTONS, (":hover",), " background-color: #e6effa; ")
    + _rule(_PRINT_BUTTONS, (":disabled",), " background-color: rgba(255, 255, 255, 0.5); ")
    + """.doc-tabs tab { padding: 2px 4px 2px 12px; min-height: 26px; }
.doc-tabs tab:checked { box-shadow: inset 0 -3px #165aa7; }
.doc-tabs .tab-close { padding: 2px; min-height: 18px; min-width: 18px; }
.new-tab { margin: 2px 6px; }
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
.update-bar {
    background-color: alpha(@theme_selected_bg_color, 0.15);
    border-bottom: 1px solid alpha(@theme_fg_color, 0.12);
    padding: 6px 6px 6px 12px;
}
.group-start { border-top: 1px solid alpha(@theme_fg_color, 0.15); padding-top: 6px; }
.menu-item { padding: 4px 10px; }
.menu-item label { font-weight: normal; }
""")

# ---- layout -----------------------------------------------------------------

def _along(box: Gtk.Box) -> bool:
    return box.get_orientation() == Gtk.Orientation.HORIZONTAL


def _place(box: Gtk.Box, child: Gtk.Widget, expand: bool, fill: bool, padding: int):
    if expand:
        if _along(box):
            child.set_hexpand(True)
            if not fill:
                child.set_halign(Gtk.Align.CENTER)
        else:
            child.set_vexpand(True)
            if not fill:
                child.set_valign(Gtk.Align.CENTER)
    if padding:
        if _along(box):
            child.set_margin_start(child.get_margin_start() + padding)
            child.set_margin_end(child.get_margin_end() + padding)
        else:
            child.set_margin_top(child.get_margin_top() + padding)
            child.set_margin_bottom(child.get_margin_bottom() + padding)


def pack(box: Gtk.Box, child: Gtk.Widget, expand: bool = False, fill: bool = True,
         padding: int = 0) -> None:
    """Add child after the box's other start children (GTK 3's pack_start)."""
    _place(box, child, expand, fill, padding)
    spacer = getattr(box, "end_spacer", None)
    if spacer is None:
        box.append(child)
    else:
        box.insert_child_after(child, spacer.get_prev_sibling())


def pack_end(box: Gtk.Box, child: Gtk.Widget, expand: bool = False, fill: bool = True,
             padding: int = 0) -> None:
    """Add child at the far end of the box, before earlier end children (GTK 3's
    pack_end)."""
    _place(box, child, expand, fill, padding)
    spacer = getattr(box, "end_spacer", None)
    if spacer is None:
        spacer = Gtk.Box(hexpand=_along(box), vexpand=not _along(box))
        box.append(spacer)
        box.end_spacer = spacer
    box.insert_child_after(child, spacer)


def children(widget: Gtk.Widget) -> List[Gtk.Widget]:
    out = []
    child = widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def rows(listbox: Gtk.ListBox) -> List[Gtk.ListBoxRow]:
    """A list box's rows (its placeholder is a child too, but not a row)."""
    out, i = [], 0
    while (row := listbox.get_row_at_index(i)) is not None:
        out.append(row)
        i += 1
    return out


def clear(container: Gtk.Widget) -> None:
    """Remove every child of a box, grid, list box or flow box."""
    if isinstance(container, Gtk.ListBox):
        for row in rows(container):
            container.remove(row)
        return
    for child in children(container):
        container.remove(child)


def margins(widget: Gtk.Widget, n: int) -> Gtk.Widget:
    widget.set_margin_top(n)
    widget.set_margin_bottom(n)
    widget.set_margin_start(n)
    widget.set_margin_end(n)
    return widget


def icon(name: str, size: int = 16) -> Gtk.Image:
    img = Gtk.Image.new_from_icon_name(name)
    if size != 16:
        img.set_pixel_size(size)
    return img


def dim_label(text: str, wrap: bool = True, xalign: float = 0) -> Gtk.Label:
    lab = Gtk.Label(label=text, xalign=xalign, wrap=wrap, max_width_chars=34)
    lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lab.add_css_class("dim")
    return lab


def icon_button(icon_name: str, tip: str, cb: Callable[[], None], flat: bool = True) -> Gtk.Button:
    b = Gtk.Button.new_from_icon_name(icon_name)
    b.set_tooltip_text(tip)
    if flat:
        b.set_has_frame(False)
    b.connect("clicked", lambda *_: cb())
    return b


# ---- drop-down lists --------------------------------------------------------

class Choice(Gtk.Box):
    """A drop-down list of (id, text) items with GTK 3 ComboBoxText's ways: append(),
    set_active_id(), get_active_id() and a "changed" signal. Items listed in
    `lines_above` start a group: the open list draws a line above them."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, items: Sequence[Tuple[str, str]] = (), lines_above=()):
        super().__init__()
        self._ids: List[str] = []
        self._model = Gtk.StringList()
        self._quiet = True
        self.dropdown = Gtk.DropDown(model=self._model, hexpand=True)
        self._lines = set(lines_above)
        # The button shortens long names, so a panel isn't as wide as its longest
        # choice; the open list shows them whole.
        self.dropdown.set_factory(self._factory(ellipsize=True))
        self.dropdown.set_list_factory(self._factory(ellipsize=False))
        Gtk.Box.append(self, self.dropdown)
        for key, text in items:
            self.append(key, text)
        self._quiet = False
        self.dropdown.connect("notify::selected", self._on_selected)

    def _factory(self, ellipsize: bool) -> Gtk.ListItemFactory:
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item):
            lab = Gtk.Label(xalign=0)
            if ellipsize:
                lab.set_ellipsize(Pango.EllipsizeMode.END)
                lab.set_width_chars(4)
            item.set_child(lab)
        factory.connect("setup", setup)
        factory.connect("bind", self._bind)
        return factory

    def _bind(self, _factory, item):
        lab = item.get_child()
        lab.set_text(item.get_item().get_string())
        if self._ids[item.get_position()] in self._lines:
            lab.add_css_class("group-start")
        else:
            lab.remove_css_class("group-start")

    def append(self, key, text: str) -> None:  # type: ignore[override]
        self._quiet = True
        self._ids.append(str(key))
        self._model.append(text)
        self._quiet = False

    def set_active_id(self, key) -> bool:
        key = str(key)
        if key not in self._ids:
            return False
        self.dropdown.set_selected(self._ids.index(key))
        return True

    def get_active_id(self) -> Optional[str]:
        i = self.dropdown.get_selected()
        return self._ids[i] if 0 <= i < len(self._ids) else None

    def _on_selected(self, *_):
        if not self._quiet:
            self.emit("changed")


def entry_with_choices(items: Sequence[str], **entry_props) -> Tuple[Gtk.Widget, Gtk.Entry]:
    """An entry with a button listing suggestions (GTK 3's combo box with an entry).
    Returns (widget to place, entry)."""
    box = Gtk.Box(hexpand=True)
    box.add_css_class("linked")
    entry = Gtk.Entry(hexpand=True, **entry_props)
    box.append(entry)
    button = Gtk.MenuButton(icon_name="pan-down-symbolic", sensitive=bool(items))
    pop = Gtk.Popover()
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    for text in items:
        b = menu_item(text, lambda t=text: (entry.set_text(t), pop.popdown()))
        col.append(b)
    pop.set_child(col)
    button.set_popover(pop)
    box.append(button)
    return box, entry


# ---- menus ------------------------------------------------------------------

def menu_item(text: str, cb: Callable[[], None]) -> Gtk.Button:
    b = Gtk.Button(has_frame=False)
    b.set_child(Gtk.Label(label=text, xalign=0))
    b.add_css_class("menu-item")
    b.connect("clicked", lambda *_: cb())
    return b


def popup_menu(widget: Gtk.Widget, x: float, y: float, items) -> Gtk.Popover:
    """A menu at (x, y) in widget's coordinates. items: (text, callback) pairs, or
    None for a separator. Each widget keeps one menu popover and refills it."""
    pop = getattr(widget, "menu_popover", None)
    if pop is None:
        pop = Gtk.Popover(has_arrow=False, halign=Gtk.Align.START)
        pop.add_css_class("menu")
        pop.set_parent(widget)
        widget.menu_popover = pop
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    for it in items:
        if it is None:
            col.append(margins(Gtk.Separator(), 2))
            continue
        text, cb = it

        def chosen(cb=cb):
            pop.popdown()
            GLib.idle_add(lambda: cb() and False)
        col.append(menu_item(text, chosen))
    pop.set_child(col)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    pop.set_pointing_to(rect)
    pop.set_offset(0, 6)
    pop.popup()
    return pop


# ---- waiting for an answer --------------------------------------------------

def _wait(start: Callable[[Callable], None], finish: Callable):
    """Run an async GTK call to the end while the rest of the app keeps working, and
    return its result (None when it was cancelled or failed)."""
    loop = GLib.MainLoop()
    out = {}

    def done(_src, result):
        try:
            out["value"] = finish(result)
        except GLib.Error:
            out["value"] = None
        loop.quit()
    start(done)
    loop.run()
    return out.get("value")


def alert(parent: Gtk.Window, title: str, detail: str, buttons: Sequence[str],
          cancel: int = -1, default: int = -1) -> int:
    """A message with buttons; returns the index of the button pressed (cancel when
    the message is dismissed)."""
    dlg = Gtk.AlertDialog(message=title, detail=detail, buttons=list(buttons), modal=True)
    dlg.set_cancel_button(cancel)
    dlg.set_default_button(default)
    r = _wait(lambda done: dlg.choose(parent, None, done), dlg.choose_finish)
    return cancel if r is None else r


def _filters(dlg: Gtk.FileDialog, filters) -> None:
    """filters: (name, [patterns] or callable(Gtk.FileFilter)) pairs; the first is the
    default."""
    if not filters:
        return
    store = Gio.ListStore.new(Gtk.FileFilter)
    for name, patterns in filters:
        f = Gtk.FileFilter()
        f.set_name(name)
        if callable(patterns):
            patterns(f)
        else:
            for pat in patterns:
                f.add_pattern(pat)
        store.append(f)
    dlg.set_filters(store)
    dlg.set_default_filter(store.get_item(0))


def choose_open(parent: Gtk.Window, title: str, filters=(), folder: Optional[str] = None,
                multiple: bool = False) -> List[str]:
    """Paths picked to open (empty when cancelled)."""
    dlg = Gtk.FileDialog(title=title, modal=True)
    _filters(dlg, filters)
    if folder:
        dlg.set_initial_folder(Gio.File.new_for_path(folder))
    if multiple:
        files = _wait(lambda done: dlg.open_multiple(parent, None, done),
                      dlg.open_multiple_finish)
        found = [files.get_item(i) for i in range(files.get_n_items())] if files else []
    else:
        one = _wait(lambda done: dlg.open(parent, None, done), dlg.open_finish)
        found = [one] if one else []
    return [f.get_path() for f in found if f.get_path()]


def choose_save(parent: Gtk.Window, title: str, name: str, filters=(),
                folder: Optional[str] = None) -> Optional[str]:
    """A path to save to (None when cancelled); the chooser asks before replacing."""
    dlg = Gtk.FileDialog(title=title, modal=True, initial_name=name)
    _filters(dlg, filters)
    if folder:
        dlg.set_initial_folder(Gio.File.new_for_path(folder))
    f = _wait(lambda done: dlg.save(parent, None, done), dlg.save_finish)
    return f.get_path() if f else None


def open_uri(parent: Gtk.Window, uri: str) -> None:
    Gtk.UriLauncher.new(uri).launch(parent, None, None, None)


class Dialog(Gtk.Window):
    """A modal window with buttons in its header bar and run(), which waits for a
    response like GTK 3's Gtk.Dialog did."""

    def __init__(self, parent: Gtk.Window, title: str, buttons=()):
        super().__init__(title=title, transient_for=parent, modal=True,
                         destroy_with_parent=True)
        self.header = Gtk.HeaderBar(show_title_buttons=not buttons)
        self.set_titlebar(self.header)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        # As tall as the content by nature. (Without the scrolled window GTK takes the
        # height of wrapped text at its narrowest as the least the window may have,
        # more than it needs at its real width, and warns.)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True,
                                      propagate_natural_width=True)
        scroller.set_child(self._content)
        self.set_child(scroller)
        self._buttons = {}
        self._response = Gtk.ResponseType.NONE
        self._loop: Optional[GLib.MainLoop] = None
        if buttons:
            self.add_buttons(*buttons)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(lambda *_: self.response(Gtk.ResponseType.CANCEL) or True)))
        self.add_controller(keys)
        self.connect("close-request", self._on_close)

    def add_buttons(self, *pairs) -> None:
        for text, response in zip(pairs[::2], pairs[1::2]):
            b = Gtk.Button(label=text)
            b.connect("clicked", lambda _b, r=response: self.response(r))
            if response == Gtk.ResponseType.CANCEL:
                self.header.pack_start(b)
            else:
                self.header.pack_end(b)
            self._buttons[response] = b
        self.header.set_show_title_buttons(False)

    def get_widget_for_response(self, response) -> Optional[Gtk.Button]:
        return self._buttons.get(response)

    def set_default_response(self, response) -> None:
        b = self._buttons.get(response)
        if b is not None:
            self.set_default_widget(b)

    def get_content_area(self) -> Gtk.Box:
        return self._content

    def response(self, response) -> None:
        self._response = response
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()

    def _on_close(self, *_):
        self.response(Gtk.ResponseType.DELETE_EVENT)
        return True            # run() returns; the caller destroys the dialog

    def run(self):
        self._response = Gtk.ResponseType.NONE
        self._loop = GLib.MainLoop()
        self.present()
        self._loop.run()
        self._loop = None
        return self._response


class PropertyForm(Gtk.Box):
    """A column of cards with labelled inputs bound to object attributes.

    Lengths are stored in dots and shown in the chosen unit.
    """

    def __init__(self, on_change: Callable[[str], None], unit: str = "mm", dpi: int = 203):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        margins(self, 10)
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
            lab.add_css_class("card-title")
            self.append(lab)
        self.grid = margins(Gtk.Grid(column_spacing=10, row_spacing=8, hexpand=True), 10)
        # vertical, so the grid's wrapped hints get the card's whole width when GTK
        # works out its height
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        card.append(self.grid)
        self.append(card)
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
        frame = Gtk.ScrolledWindow(min_content_height=70, has_frame=True)
        frame.set_child(tv)

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
        c = Choice([(str(v), lab) for v, lab in options])
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
        box.add_css_class("linked")
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
            b = Gtk.ToggleButton(icon_name=icon, hexpand=True)
            b.set_tooltip_text(tip)
            b.set_active(getattr(obj, attr) == value)
            b.connect("toggled", clicked, value)
            buttons.append((b, value))
            box.append(b)
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
        box.add_css_class("toast")
        self.label = Gtk.Label(xalign=0, wrap=True, max_width_chars=60, hexpand=True)
        self.action = Gtk.Button(visible=False)
        self.action.connect("clicked", self._on_action)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.connect("clicked", lambda *_: self.dismiss())
        box.append(self.label)
        box.append(self.action)
        box.append(close)
        self.set_child(box)
        self._cb: Optional[Callable[[], None]] = None
        self._timer = 0

    def show_message(self, text: str, action: Optional[str] = None,
                     callback: Optional[Callable[[], None]] = None, seconds: int = 4):
        self.label.set_text(text)
        self._cb = callback
        if action:
            self.action.set_label(action)
            self.action.set_visible(True)
        else:
            self.action.set_visible(False)
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
