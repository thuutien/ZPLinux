"""Cairo rendering of a label, in printer dots, as close to the printer as practical."""

from __future__ import annotations

import hashlib
from typing import Dict, Optional, Tuple

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Pango, PangoCairo  # noqa: E402

from . import barcode, symbology, zint  # noqa: E402
from .model import (BarcodeElement, BoxElement, Code128Element, EllipseElement,  # noqa: E402
                    ImageElement, Label, QRElement, TextElement, dpmm, interpretation_height)

# Closest common stand-in for Zebra font 0 (CG Triumvirate Bold Condensed).
FONT_FAMILY = "Liberation Sans Narrow, Nimbus Sans Narrow, Ubuntu Condensed, DejaVu Sans Condensed, Sans"
# Font 0 cell height vs. the font's em size, tuned against Labelary output.
EM_PER_HEIGHT = 1.0
# Font 0 is a little narrower than its height at width == height.
WIDTH_FACTOR = 1.02

_image_cache: Dict[Tuple, cairo.ImageSurface] = {}


def _font_layout(cr, text: str, height: int, bold: bool = True) -> Pango.Layout:
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string(FONT_FAMILY)
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    desc.set_absolute_size(height * EM_PER_HEIGHT * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    return layout


def draw_text(cr, el: TextElement) -> None:
    h = el.height
    xscale = (el.width or h) / h * WIDTH_FACTOR
    layout = _font_layout(cr, el.text, h)
    # Line pitch equals the character height, as on the printer.
    layout.set_spacing(0)
    if el.block_width:
        layout.set_width(int(el.block_width / xscale * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_alignment({"C": Pango.Alignment.CENTER, "R": Pango.Alignment.RIGHT}
                             .get(el.align, Pango.Alignment.LEFT))
        layout.set_justify(el.align == "J")
        if el.max_lines > 0:
            # Zebra drops lines beyond the limit (it overprints the last one; close enough).
            layout.set_height(-max(1, el.max_lines))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
    else:
        layout.set_text(el.text.replace("\n", " "), -1)
    _, logical = layout.get_pixel_extents()
    lines = max(1, layout.get_line_count())
    scale_y = h * lines / max(1, logical.height)
    el.measured_w = el.block_width or max(1, round(logical.width * xscale))
    el.measured_h = h * lines

    cr.save()
    cr.translate(el.x, el.y)
    cr.scale(xscale, scale_y)
    if el.reverse:
        cr.set_operator(cairo.OPERATOR_DIFFERENCE)
        cr.set_source_rgb(1, 1, 1)
    else:
        cr.set_source_rgb(0, 0, 0)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def draw_code128(cr, el: Code128Element) -> None:
    widths = barcode.code128(el.data)
    cr.save()
    cr.set_source_rgb(0, 0, 0)
    if widths is None:
        # Not encodable in Code 128 subsets B/C: show a hatched placeholder.
        w = barcode.code128_modules(el.data) * el.module
        el.measured_w = w
        cr.rectangle(el.x, el.y, w, el.height)
        cr.set_line_width(2)
        cr.stroke()
        cr.move_to(el.x, el.y)
        cr.line_to(el.x + w, el.y + el.height)
        cr.stroke()
        cr.restore()
        return
    x = el.x
    for i, mod in enumerate(widths):
        if i % 2 == 0:
            cr.rectangle(x, el.y, mod * el.module, el.height)
        x += mod * el.module
    cr.fill()
    el.measured_w = x - el.x
    if el.show_text and el.text_size:
        # A font 0 line, centered under the bars, as written to the ZPL.
        s = el.text_size
        layout = _font_layout(cr, el.data, s)
        _, logical = layout.get_pixel_extents()
        sy = s / max(1, logical.height)
        tw = logical.width * WIDTH_FACTOR
        cr.translate(el.x + (el.measured_w - tw) / 2, el.y + el.height + el.text_gap())
        cr.scale(WIDTH_FACTOR, sy)
        PangoCairo.show_layout(cr, layout)
    elif el.show_text:
        th = interpretation_height(el.module)
        layout = _font_layout(cr, el.data, th, bold=False)
        _, logical = layout.get_pixel_extents()
        # The printer's interpretation font is smaller than its line slot.
        sy = th * 0.8 / max(1, logical.height)
        tw = logical.width * sy
        cr.translate(el.x + (el.measured_w - tw) / 2,
                     el.y + el.height + el.text_gap() + th * 0.15)
        cr.scale(sy, sy)
        PangoCairo.show_layout(cr, layout)
    cr.restore()


def _barcode_text(cr, el, text: str, left: float, width: float, top: float) -> float:
    """The printer's small interpretation line, centered under the bars; returns its height."""
    th = interpretation_height(el.module)
    layout = _font_layout(cr, text, th, bold=False)
    _, logical = layout.get_pixel_extents()
    sy = th * 0.8 / max(1, logical.height)
    cr.save()
    cr.translate(left + (width - logical.width * sy) / 2, top + th * 0.15)
    cr.scale(sy, sy)
    PangoCairo.show_layout(cr, layout)
    cr.restore()
    return th


def _placeholder(cr, el, message: str) -> None:
    """A hatched box where zint could not draw the barcode."""
    w, h = 220, max(40, el.height if el.height < 400 else 80)
    el.measured_w, el.measured_h = w, h
    cr.rectangle(el.x, el.y, w, h)
    cr.set_line_width(2)
    cr.stroke()
    cr.move_to(el.x, el.y)
    cr.line_to(el.x + w, el.y + h)
    cr.stroke()
    sym = symbology.get(el.symbology)
    layout = _font_layout(cr, f"{sym.name if sym else el.symbology}\n{message}", 14, bold=False)
    layout.set_width(int((w - 8) * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    cr.move_to(el.x + 4, el.y + 4)
    PangoCairo.show_layout(cr, layout)


def _x_map(sym: zint.Symbol, spec, el) -> Dict[float, float]:
    """Module positions -> dots. Two-width codes use the ^BY ratio for wide bars and
    spaces (rounded down, as the printer does); postal codes have wider gaps."""
    edges = sorted({x for x, _y, w, _h in sym.rects} | {x + w for x, _y, w, _h in sym.rects}
                   | {0.0, sym.width})
    m = el.module
    if spec.two_width:
        wide = int(el.ratio * m)
        unit = lambda run: m if run <= 1 else wide
    elif spec.shape == "postal":
        bars = {(x, x + w) for x, _y, w, _h in sym.rects}
        gap = int(m * 1.5)
        # measured: when 1.5 x module isn't whole, the printer's first gap is a dot narrower
        first = gap - (1 if m % 2 else 0)
        out, pos = {}, 0.0
        for a, b in zip(edges, edges[1:]):
            out[a] = pos
            pos += m if (a, b) in bars else (first if a == edges[1] else gap) * (b - a)
        out[edges[-1]] = pos
        return out
    else:
        unit = lambda run: run * m
    out, pos = {}, 0.0
    for a, b in zip(edges, edges[1:]):
        out[a] = pos
        pos += unit(b - a)
    out[edges[-1]] = pos
    return out


def draw_barcode(cr, el, dots_per_mm: int = 8) -> None:
    spec = symbology.get(el.symbology)
    cr.save()
    cr.set_source_rgb(0, 0, 0)
    if spec is None:
        _placeholder(cr, el, "Unknown barcode type")
        cr.restore()
        return
    try:
        sym = zint.encode(spec.zint_type_for(el), spec.zint_data(el.data, el),
                          tuple(spec.zint_options(el)))
    except zint.ZintError as e:
        _placeholder(cr, el, str(e))
        cr.restore()
        return
    m = el.module
    if spec.shape == "maxicode":
        # MaxiCode has a fixed size whatever the module setting: the printer's ink
        # covers 25 x 24.125 mm (measured at 203 dpi); fit zint's hexagons to that.
        pts = [p for hexagon in sym.hexagons for p in hexagon]
        x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
        sx = dots_per_mm * 25.0 / (max(p[0] for p in pts) - x0)
        sy = dots_per_mm * 24.125 / (max(p[1] for p in pts) - y0)
        tx = lambda px: el.x + (px - x0) * sx
        ty = lambda py: el.y + (py - y0) * sy
        for hexagon in sym.hexagons:
            cr.move_to(tx(hexagon[0][0]), ty(hexagon[0][1]))
            for px, py in hexagon[1:]:
                cr.line_to(tx(px), ty(py))
            cr.close_path()
        cr.fill()
        for cx, cy, r, stroke in sym.circles:
            cr.new_sub_path()
            cr.arc(tx(cx), ty(cy), r * sx, 0, 6.2832)
        if sym.circles:
            cr.set_line_width(sym.circles[0][3] * sx)
            cr.stroke()
        el.measured_w, el.measured_h = round(25.0 * dots_per_mm), round(24.125 * dots_per_mm)
        cr.restore()
        return

    if spec.shape in ("matrix", "scaled"):
        xs = lambda x: x * m
        ys = lambda y: y * m
        total_h = sym.height * m
    elif spec.shape == "stacked":
        heights = [h for _x, _y, _w, h in sym.rects]
        row = max(set(heights), key=heights.count)      # the usual row height
        xs = lambda x: x * m
        ys = lambda y: y * el.height / row
        total_h = sym.height * el.height / row
    else:   # linear and postal: the height setting is the bar height
        xm = _x_map(sym, spec, el)
        xs = lambda x: xm[x]
        base = min(h for _x, _y, _w, h in sym.rects)    # EAN/UPC guard bars are longer
        total_h = el.height
    for x, y, w, h in sym.rects:
        x0, x1 = xs(x), xs(x + w)
        if spec.shape == "linear":
            # bars full height; EAN/UPC guard bars reach lower by their extra modules
            y0, y1 = 0, el.height + max(0, h - base) * m
        elif spec.shape == "postal":
            # Zebra's short postal bars are 2/5 of the full height (zint's are half)
            y0, y1 = (0 if h >= sym.height else el.height - round(el.height * 0.4)), el.height
        else:
            y0, y1 = ys(y), ys(y + h)
        cr.rectangle(el.x + x0, el.y + y0, x1 - x0, y1 - y0)
    cr.fill()
    width = xs(sym.width) if spec.shape not in ("matrix", "scaled", "stacked") else sym.width * m
    if spec.shape in ("linear", "postal") and (spec.always_text or
                                                (el.show_text and "show_text" in spec.options)):
        # below the EAN/UPC guard bars, which reach lower than the others
        top = el.height + (max(h for *_r, h in sym.rects) - base) * m + max(2, m)
        total_h = top + _barcode_text(cr, el, symbology.display_text(el), el.x, width,
                                      el.y + top)
    el.measured_w, el.measured_h = round(width), round(total_h)
    cr.restore()


def draw_qr(cr, el: QRElement) -> None:
    m = el.magnification
    matrix = barcode.qr_matrix(el.data, el.error_correction)
    cr.save()
    cr.set_source_rgb(0, 0, 0)
    if matrix is None:
        size = 25 * m
        cr.rectangle(el.x, el.y, size, size)
        cr.set_line_width(2)
        cr.stroke()
        layout = _font_layout(cr, "QR", size // 3)
        cr.move_to(el.x + size // 4, el.y + size // 3)
        PangoCairo.show_layout(cr, layout)
        el.measured_w = size
    else:
        for r, row in enumerate(matrix):
            for c, dark in enumerate(row):
                if dark:
                    cr.rectangle(el.x + c * m, el.y + r * m, m, m)
        cr.fill()
        el.measured_w = len(matrix) * m
    cr.restore()


def draw_box(cr, el: BoxElement) -> None:
    cr.save()
    cr.set_source_rgb(0, 0, 0)
    t = min(el.w, el.h) if el.filled else max(1, min(el.thickness, el.w, el.h))
    # ZPL rounding 0-8 is in eighths of the shorter side / 2.
    radius = el.rounding / 8 * min(el.w, el.h) / 2
    _rounded_rect(cr, el.x, el.y, el.w, el.h, radius)
    if t * 2 < min(el.w, el.h):
        _rounded_rect(cr, el.x + t, el.y + t, el.w - 2 * t, el.h - 2 * t, max(0, radius - t))
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    cr.fill()
    cr.restore()


def _rounded_rect(cr, x, y, w, h, r):
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    import math
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw_ellipse(cr, el: EllipseElement) -> None:
    import math
    cr.save()
    cr.set_source_rgb(0, 0, 0)

    def ellipse(x, y, w, h):
        cr.save()
        cr.translate(x + w / 2, y + h / 2)
        cr.scale(max(w, 0.01) / 2, max(h, 0.01) / 2)
        cr.new_sub_path()
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()

    ellipse(el.x, el.y, el.w, el.h)
    t = el.thickness
    if not el.filled and t * 2 < min(el.w, el.h):
        ellipse(el.x + t, el.y + t, el.w - 2 * t, el.h - 2 * t)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    cr.fill()
    cr.restore()


def image_surface(el: ImageElement) -> Optional[cairo.ImageSurface]:
    """The image as it will print: scaled and thresholded to black/transparent."""
    raw = el.image_bytes()
    if not raw:
        return None
    key = (hashlib.sha1(raw).hexdigest(), el.w, el.h, el.threshold)
    if key in _image_cache:
        return _image_cache[key]
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(raw)
        loader.close()
    except Exception:
        return None
    pb = loader.get_pixbuf().scale_simple(el.w, el.h, GdkPixbuf.InterpType.BILINEAR)
    w, h = pb.get_width(), pb.get_height()
    n, stride, px = pb.get_n_channels(), pb.get_rowstride(), pb.get_pixels()
    has_alpha = pb.get_has_alpha()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    sstride = surf.get_stride()
    data = surf.get_data()
    black = b"\x00\x00\x00\xff"
    for y in range(h):
        base = y * stride
        for x in range(w):
            i = base + x * n
            lum = (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) // 1000
            a = px[i + 3] if has_alpha else 255
            lum = 255 - (255 - lum) * a // 255
            if lum < el.threshold:
                o = y * sstride + x * 4
                data[o:o + 4] = black
    surf.mark_dirty()
    if len(_image_cache) > 32:
        _image_cache.clear()
    _image_cache[key] = surf
    return surf


def draw_image(cr, el: ImageElement) -> None:
    surf = image_surface(el)
    cr.save()
    if surf is None:
        cr.set_source_rgb(0.6, 0, 0)
        cr.rectangle(el.x, el.y, el.w, el.h)
        cr.set_line_width(2)
        cr.stroke()
    else:
        cr.set_source_surface(surf, el.x, el.y)
        pattern = cr.get_source()
        pattern.set_filter(cairo.FILTER_NEAREST)
        cr.paint()
    cr.restore()


DRAWERS = {
    TextElement: draw_text,
    Code128Element: draw_code128,
    QRElement: draw_qr,
    BoxElement: draw_box,
    EllipseElement: draw_ellipse,
    ImageElement: draw_image,
}


def draw_label(cr, label: Label) -> None:
    """Draw white paper and every element. Coordinates are dots."""
    cr.save()
    cr.rectangle(0, 0, label.width_dots, label.height_dots)
    cr.set_source_rgb(1, 1, 1)
    cr.fill_preserve()
    cr.clip()
    for el in label.elements:
        if isinstance(el, BarcodeElement):
            draw_barcode(cr, el, dpmm(label.dpi))
        else:
            DRAWERS[type(el)](cr, el)
    cr.restore()


def render_png(label: Label, path: str) -> None:
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, label.width_dots, label.height_dots)
    cr = cairo.Context(surf)
    draw_label(cr, label)
    surf.write_to_png(path)
