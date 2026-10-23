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

from . import barcode  # noqa: E402
from .model import (BoxElement, Code128Element, EllipseElement, ImageElement, Label,  # noqa: E402
                    QRElement, TextElement, interpretation_height)

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
        DRAWERS[type(el)](cr, el)
    cr.restore()


def render_png(label: Label, path: str) -> None:
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, label.width_dots, label.height_dots)
    cr = cairo.Context(surf)
    draw_label(cr, label)
    surf.write_to_png(path)
