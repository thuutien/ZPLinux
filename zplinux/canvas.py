"""The design surface: draws the label and handles selecting, moving and resizing."""

from __future__ import annotations

from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import render  # noqa: E402
from .model import BoxElement, Element, EllipseElement, dpmm  # noqa: E402

MARGIN = 32          # screen pixels around the work area
WORK = 160           # screen pixels of work area around the label, for objects off it
OUTSIDE_ALPHA = 0.35  # how strongly objects off the label are drawn
HANDLE = 6           # resize handle radius in screen pixels
SNAP_PX = 6          # snapping distance in screen pixels
GRID_MM = 5
ACCENT = (0.21, 0.52, 0.89)
GUIDE = (0.91, 0.2, 0.55)


class LabelCanvas(Gtk.DrawingArea):
    """`host` is the window; the canvas reads host.label / host.values / host.selected /
    host.selection and calls host.select(), host.set_selection(), host.toggle_selected(),
    host.checkpoint(), host.element_changed(), host.context_menu(),
    host.activate_element(), host.drop_files() and host.pointer_moved()."""

    def __init__(self, host):
        super().__init__(can_focus=True)
        self.host = host
        self.zoom = 1.0
        self.show_grid = False
        self.snap = True
        self.hover: Optional[Element] = None
        self.drag = None       # dict while a move/resize is in progress
        self.guides: List[Tuple[str, float]] = []   # ("v", x) / ("h", y) in dots
        self.extents = (0.0, 0.0, 1.0, 1.0)  # work area in dots: x0, y0, x1, y1
        self._center_pending = False
        self.connect("size-allocate", self._on_allocate)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("leave-notify-event", self.on_leave)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self.on_drop)

    # ---- geometry -----------------------------------------------------------

    @property
    def label(self):
        return self.host.label

    def resize_to_label(self):
        """Size the canvas for the label, the work area around it and any object
        placed further out."""
        lab = self.label
        pad = WORK / self.zoom
        x0, y0, x1, y1 = -pad, -pad, lab.width_dots + pad, lab.height_dots + pad
        for el in lab.elements:
            bx, by, bw, bh = el.bounds()
            x0, y0 = min(x0, bx - pad / 4), min(y0, by - pad / 4)
            x1, y1 = max(x1, bx + bw + pad / 4), max(y1, by + bh + pad / 4)
        self.extents = (x0, y0, x1, y1)
        self.set_size_request(int((x1 - x0) * self.zoom) + 2 * MARGIN,
                              int((y1 - y0) * self.zoom) + 2 * MARGIN)
        self.queue_draw()

    def origin(self) -> Tuple[float, float]:
        """Where dot (0, 0), the label's top-left, is in widget pixels; the work area is
        centred when it is smaller than the visible area."""
        a = self.get_allocation()
        x0, y0, x1, y1 = self.extents
        w, h = (x1 - x0) * self.zoom, (y1 - y0) * self.zoom
        return (max(MARGIN, (a.width - w) / 2) - x0 * self.zoom,
                max(MARGIN, (a.height - h) / 2) - y0 * self.zoom)

    def _keep_still(self, old_x0: float, old_y0: float):
        """After the work area grew left or up, scroll by as much so nothing moves."""
        def shift():
            sw = self.get_parent()
            while sw is not None and not isinstance(sw, Gtk.ScrolledWindow):
                sw = sw.get_parent()
            if sw is not None:
                x0, y0, _x1, _y1 = self.extents
                for adj, d in ((sw.get_hadjustment(), old_x0 - x0),
                               (sw.get_vadjustment(), old_y0 - y0)):
                    if d > 0:
                        adj.set_value(adj.get_value() + d * self.zoom)
            return False
        GLib.idle_add(shift)

    def center_on_label(self):
        """Scroll so the label is in the middle of the view, once the canvas has its new
        size (or now, if the size doesn't change)."""
        self._center_pending = True
        GLib.timeout_add(150, self._center_if_pending)

    def _on_allocate(self, _w, _a):
        if self._center_pending:
            GLib.idle_add(self._center_if_pending)

    def _center_if_pending(self):
        if self._center_pending:
            self._center_pending = False
            self._center()
        return False

    def _center(self):
        sw = self.get_parent()
        while sw is not None and not isinstance(sw, Gtk.ScrolledWindow):
            sw = sw.get_parent()
        if sw is None:
            return
        ox, oy = self.origin()
        for adj, mid in ((sw.get_hadjustment(), ox + self.label.width_dots * self.zoom / 2),
                         (sw.get_vadjustment(), oy + self.label.height_dots * self.zoom / 2)):
            adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(),
                                                   mid - adj.get_page_size() / 2)))

    def to_dots(self, ex: float, ey: float) -> Tuple[float, float]:
        ox, oy = self.origin()
        return (ex - ox) / self.zoom, (ey - oy) / self.zoom

    def hit(self, x: float, y: float) -> Optional[Element]:
        pad = 4 / self.zoom
        for el in reversed(self.label.elements):
            bx, by, bw, bh = el.bounds()
            if not (bx - pad <= x <= bx + bw + pad and by - pad <= y <= by + bh + pad):
                continue
            if isinstance(el, (BoxElement, EllipseElement)) and not el.filled:
                # an outline: only its edge, so things inside a frame can be picked
                # and a selection box can start inside it
                edge = el.thickness + pad
                if bx + edge < x < bx + bw - edge and by + edge < y < by + bh - edge:
                    continue
            return el
        return None

    def on_handle(self, ex: float, ey: float) -> bool:
        el = self.host.selected
        if not el or len(self.host.selection) != 1:
            return False
        x, y, w, h = el.bounds()
        ox, oy = self.origin()
        hx, hy = ox + (x + w) * self.zoom + 2, oy + (y + h) * self.zoom + 2
        return (ex - hx) ** 2 + (ey - hy) ** 2 <= (HANDLE + 4) ** 2

    # ---- drawing ------------------------------------------------------------

    def on_draw(self, widget, cr):
        lab = self.label
        ox, oy = self.origin()
        w, h = lab.width_dots * self.zoom, lab.height_dots * self.zoom
        cr.translate(ox, oy)

        # Soft shadow under the paper
        for i, alpha in ((6, 0.06), (3, 0.08), (1, 0.12)):
            cr.set_source_rgba(0, 0, 0, alpha)
            cr.rectangle(-i + 1, -i + 3, w + 2 * i - 2, h + 2 * i - 2)
            cr.fill()

        cr.save()
        cr.scale(self.zoom, self.zoom)
        preview = lab.filled(self.host.values)
        # Objects off the label are drawn faded (they don't print), then the label
        # itself as it prints.
        cr.push_group()
        render.draw_elements(cr, preview)
        cr.pop_group_to_source()
        cr.paint_with_alpha(OUTSIDE_ALPHA)
        render.draw_label(cr, preview)
        # Measured sizes come from the preview; copy them back for hit-testing.
        for src, dst in zip(preview.elements, lab.elements):
            for attr in ("measured_w", "measured_h"):
                if hasattr(src, attr):
                    setattr(dst, attr, getattr(src, attr))
        cr.restore()

        if self.show_grid:
            step = GRID_MM * dpmm(lab.dpi) * self.zoom
            cr.set_source_rgba(*ACCENT, 0.18)
            cr.set_line_width(1)
            x = step
            while x < w:
                cr.move_to(int(x) + 0.5, 0)
                cr.line_to(int(x) + 0.5, h)
                x += step
            y = step
            while y < h:
                cr.move_to(0, int(y) + 0.5)
                cr.line_to(w, int(y) + 0.5)
                y += step
            cr.stroke()

        if self.guides:
            cr.set_source_rgb(*GUIDE)
            cr.set_line_width(1)
            for kind, pos in self.guides:
                p = int(pos * self.zoom) + 0.5
                if kind == "v":
                    cr.move_to(p, -MARGIN / 2)
                    cr.line_to(p, h + MARGIN / 2)
                else:
                    cr.move_to(-MARGIN / 2, p)
                    cr.line_to(w + MARGIN / 2, p)
            cr.stroke()

        if self.hover is not None and not self.host.is_selected(self.hover) \
                and self.hover in lab.elements and not self.drag:
            x, y, bw, bh = (v * self.zoom for v in self.hover.bounds())
            cr.set_source_rgba(*ACCENT, 0.5)
            cr.set_line_width(1)
            cr.rectangle(int(x) - 1.5, int(y) - 1.5, int(bw) + 3, int(bh) + 3)
            cr.stroke()

        selection = self.host.selection
        for el in selection:
            x, y, bw, bh = (v * self.zoom for v in el.bounds())
            cr.set_source_rgba(*ACCENT, 1)
            cr.set_line_width(1.5)
            cr.rectangle(int(x) - 2, int(y) - 2, int(bw) + 4, int(bh) + 4)
            cr.stroke()
        if len(selection) == 1:          # resizing is for one object at a time
            import math
            x, y, bw, bh = (v * self.zoom for v in selection[0].bounds())
            cr.arc(x + bw + 2, y + bh + 2, HANDLE, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(2)
            cr.stroke()

        if self.drag and self.drag["mode"] == "band" and self.drag["started"]:
            x0, y0, x1, y1 = self._band()
            cr.rectangle(x0 * self.zoom, y0 * self.zoom, (x1 - x0) * self.zoom,
                         (y1 - y0) * self.zoom)
            cr.set_source_rgba(*ACCENT, 0.12)
            cr.fill_preserve()
            cr.set_source_rgba(*ACCENT, 0.9)
            cr.set_line_width(1)
            cr.stroke()
        return False

    def _band(self):
        d = self.drag
        return (min(d["x"], d["cx"]), min(d["y"], d["cy"]),
                max(d["x"], d["cx"]), max(d["y"], d["cy"]))

    # ---- snapping -----------------------------------------------------------

    def _targets(self, moving: Element):
        lab = self.label
        xs = [0, lab.width_dots / 2, lab.width_dots]
        ys = [0, lab.height_dots / 2, lab.height_dots]
        for el in lab.elements:
            if el is moving or self.host.is_selected(el):
                continue
            bx, by, bw, bh = el.bounds()
            xs += [bx, bx + bw / 2, bx + bw]
            ys += [by, by + bh / 2, by + bh]
        return xs, ys

    def _snap_axis(self, pos: float, offsets, targets, kind: str) -> float:
        thresh = SNAP_PX / self.zoom
        best = None
        for off in offsets:
            for t in targets:
                d = t - (pos + off)
                if abs(d) <= thresh and (best is None or abs(d) < abs(best[0])):
                    best = (d, t)
        if best is not None:
            self.guides.append((kind, best[1]))
            return pos + best[0]
        if self.show_grid:
            step = GRID_MM * dpmm(self.label.dpi)
            return round(pos / step) * step
        return pos

    # ---- events -------------------------------------------------------------

    def on_press(self, widget, ev):
        self.grab_focus()
        x, y = self.to_dots(ev.x, ev.y)
        if ev.type == Gdk.EventType._2BUTTON_PRESS and ev.button == 1:
            el = self.hit(x, y)
            if el:
                self.host.activate_element(el)
            return True
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if ev.button == 3:
            el = self.hit(x, y)
            if el is None or not self.host.is_selected(el):
                self.host.select(el)
            self.host.context_menu(ev, el)
            return True
        if ev.button != 1:
            return False
        if self.on_handle(ev.x, ev.y):
            self.drag = {"mode": "resize", "x": x, "y": y, "orig": self.host.selected.bounds(),
                         "started": False}
            return True
        el = self.hit(x, y)
        adding = bool(ev.state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK))
        if el is None:
            # drag on empty space: select what ends up inside the box (with Shift: add it)
            self.drag = {"mode": "band", "x": x, "y": y, "cx": x, "cy": y, "started": False,
                         "keep": self.host.selection if adding else []}
            if not adding:
                self.host.select(None)
            return True
        if adding:
            self.host.toggle_selected(el)
            return True
        if not self.host.is_selected(el):
            self.host.select(el)
        elif el is not self.host.selected:
            # make the grabbed object the main one, keeping the others
            self.host.set_selection([e for e in self.host.selection if e is not el] + [el])
        self.drag = {"mode": "move", "x": x, "y": y, "orig": (el.x, el.y), "started": False,
                     "group": [(e, e.x, e.y) for e in self.host.selection if e is not el]}
        return True

    def on_motion(self, widget, ev):
        x, y = self.to_dots(ev.x, ev.y)
        self.host.pointer_moved(x, y)
        el = self.host.selected
        if self.drag and self.drag["mode"] == "band":
            d = self.drag
            d["cx"], d["cy"] = x, y
            if not d["started"] and (abs(x - d["x"]) + abs(y - d["y"])) * self.zoom < 4:
                return True
            d["started"] = True
            self.queue_draw()
            return True
        if self.drag and el:
            d = self.drag
            dx, dy = x - d["x"], y - d["y"]
            if not d["started"]:
                if abs(dx) * self.zoom < 3 and abs(dy) * self.zoom < 3:
                    return True
                self.host.checkpoint()
                d["started"] = True
            free = bool(ev.state & Gdk.ModifierType.MOD1_MASK) or not self.snap
            self.guides = []
            if d["mode"] == "move":
                _bx, _by, bw, bh = el.bounds()
                nx, ny = d["orig"][0] + dx, d["orig"][1] + dy
                if not free:
                    xs, ys = self._targets(el)
                    nx = self._snap_axis(nx, (0, bw / 2, bw), xs, "v")
                    ny = self._snap_axis(ny, (0, bh / 2, bh), ys, "h")
                # Objects may go off the label; only the part on it prints.
                el.x, el.y = int(round(nx)), int(round(ny))
                # the rest of the selection moves as much as the grabbed object did
                ddx, ddy = el.x - d["orig"][0], el.y - d["orig"][1]
                for other, ox, oy in d.get("group", ()):
                    other.x, other.y = ox + ddx, oy + ddy
            else:
                ox, oy, ow, oh = d["orig"]
                right, bottom = ox + ow + dx, oy + oh + dy
                if not free:
                    xs, ys = self._targets(el)
                    right = self._snap_axis(right, (0,), xs, "v")
                    bottom = self._snap_axis(bottom, (0,), ys, "h")
                el.resize(int(round(right - ox)), int(round(bottom - oy)))
            self.host.element_changed(el)
            return True
        hov = self.hit(x, y)
        if hov is not self.hover:
            self.hover = hov
            self.queue_draw()
        cursor = "se-resize" if self.on_handle(ev.x, ev.y) else ("move" if hov else None)
        win = self.get_window()
        if win:
            win.set_cursor(Gdk.Cursor.new_from_name(win.get_display(), cursor) if cursor else None)
        return True

    def on_leave(self, *_):
        if self.hover is not None:
            self.hover = None
            self.queue_draw()
        self.host.pointer_moved(None, None)

    def on_release(self, widget, ev):
        if self.drag and self.drag["mode"] == "band":
            d = self.drag
            x0, y0, x1, y1 = self._band()
            self.drag = None
            if d["started"]:
                touched = [e for e in self.label.elements
                           if self._inside(e.bounds(), x0, y0, x1, y1)]
                self.host.set_selection(d["keep"] + [e for e in touched
                                                     if not any(e is k for k in d["keep"])])
            self.queue_draw()
            return True
        was_drag = self.drag and self.drag["started"]
        self.drag = None
        if self.guides:
            self.guides = []
            self.queue_draw()
        if was_drag:
            self.host.drag_finished()
            x0, y0, x1, y1 = self.extents
            bx, by, bw, bh = self.host.selected.bounds() if self.host.selected else (0, 0, 0, 0)
            if bx < x0 or by < y0 or bx + bw > x1 or by + bh > y1:
                self.resize_to_label()      # make room for it ...
                self._keep_still(x0, y0)    # ... without the label jumping
        return True

    @staticmethod
    def _inside(box, x0, y0, x1, y1) -> bool:
        """A selection box picks the objects wholly inside it."""
        bx, by, bw, bh = box
        return bx >= x0 and by >= y0 and bx + bw <= x1 and by + bh <= y1

    def on_drop(self, widget, ctx, ex, ey, data, info, time):
        paths = []
        for uri in data.get_uris() or []:
            u = urlparse(uri)
            if u.scheme == "file":
                paths.append(unquote(u.path))
        x, y = self.to_dots(ex, ey)
        if paths:
            self.host.drop_files(paths, x, y)
