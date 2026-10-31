"""Turn a Label into ZPL II."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from . import barcode, symbology
from .model import (BarcodeElement, BoxElement, Code128Element, EllipseElement,
                    ImageElement, Label, QRElement, TextElement, rotate_rect)


QR_Y_SHIFT = 10


def escape(text: str) -> str:
    """Field data is sent with ^FH_ so _, ^ and ~ survive as hex escapes. The default
    _ indicator (rather than \\) leaves \\& free to mean "new line" inside ^FB."""
    return text.replace("_", "_5F").replace("^", "_5E").replace("~", "_7E")


def block_text(text: str) -> str:
    """Text for a ^FB block: a backslash is written \\\\ and a line break \\&."""
    return text.replace("\\", "\\\\").replace("\r\n", "\n").replace("\n", "\\&")


def field_data(text: str) -> str:
    return f"^FH_^FD{escape(text)}^FS"


def image_to_gf(el: ImageElement) -> Optional[str]:
    """^GFA command for an image element, scaled to its size and thresholded to 1-bit."""
    raw = el.image_bytes()
    if not raw:
        return None
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    loader = GdkPixbuf.PixbufLoader()
    loader.write(raw)
    loader.close()
    pb = loader.get_pixbuf().scale_simple(el.w, el.h, GdkPixbuf.InterpType.BILINEAR)
    if pb is None:
        return None
    turns = {90: GdkPixbuf.PixbufRotation.CLOCKWISE, 180: GdkPixbuf.PixbufRotation.UPSIDEDOWN,
             270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE}
    if el.rotation % 360 in turns:      # ^GF has no orientation: turn the image itself
        pb = pb.rotate_simple(turns[el.rotation % 360])
    w, h = pb.get_width(), pb.get_height()
    n, stride = pb.get_n_channels(), pb.get_rowstride()
    has_alpha = pb.get_has_alpha()
    px = pb.get_pixels()
    row_bytes = (w + 7) // 8
    out = []
    for y in range(h):
        row = bytearray(row_bytes)
        base = y * stride
        for x in range(w):
            i = base + x * n
            r, g, b = px[i], px[i + 1], px[i + 2]
            a = px[i + 3] if has_alpha else 255
            # Transparent pixels count as white paper.
            lum = (r * 299 + g * 587 + b * 114) // 1000
            lum = 255 - (255 - lum) * a // 255
            if lum < el.threshold:
                row[x >> 3] |= 0x80 >> (x & 7)
        out.append(row.hex().upper())
    total = row_bytes * h
    return f"^GFA,{total},{total},{row_bytes},{''.join(out)}"


ORIENTATIONS = {0: "N", 90: "R", 180: "I", 270: "B"}


def orientation(el) -> str:
    """The ZPL field orientation for an element's rotation."""
    return ORIENTATIONS.get(el.rotation % 360, "N")


def field_origin(el, anchor=None) -> str:
    """^FO for an element. A rotated field's ^FO is the top-left of its rotated anchor:
    the whole element for text, the bars for barcodes (their text then sits on the
    bars' far side), as measured on Labelary. anchor is (x, y, w, h) upright."""
    x, y = el.x, el.y
    if el.rotation % 360 and anchor is not None:
        w0, h0 = el.size()
        ax, ay, _w, _h = rotate_rect(anchor, w0, h0, el.rotation)
        x, y = x + ax, y + ay
    return f"^FO{max(0, round(x))},{max(0, round(y))}"


def barcode_text_zpl(el: Code128Element) -> str:
    """The line under a barcode when it has its own text size: font 0, centered under
    the bars in a one-line ^FB (as ZebraDesigner does), widened if the text is longer
    than the barcode so it isn't cut off."""
    widths = barcode.code128(el.data)
    bars = (sum(widths) if widths else barcode.code128_modules(el.data)) * el.module
    s = el.text_size
    # Font 0 digits and capitals are a little over half as wide as they are tall.
    block = max(bars, int(len(el.data) * s * 0.6) + s)
    line = (-((block - bars) // 2), el.height + el.text_gap(), block, s)   # upright
    if el.rotation % 360:
        fo = field_origin(el, line)
    else:
        fo = f"^FO{max(0, el.x + line[0])},{el.y + line[1]}"
    return (f"{fo}^A0{orientation(el)},{s},{s}^FB{block},1,0,C,0"
            + field_data(block_text(el.data)))


def element_zpl(el) -> List[str]:
    fo = field_origin(el)
    o = orientation(el)
    if isinstance(el, TextElement):
        h, w = el.font_size()
        cmd = f"{fo}^A{el.font if el.bitmap_font() else '0'}{o},{h},{w}"
        if el.block_width:
            cmd += f"^FB{el.block_width},{max(1, el.max_lines)},0,{el.align},0"
        if el.reverse:
            cmd += "^FR"
        # Without a text block the printer can't break lines, so keep it on one line.
        text = block_text(el.text) if el.block_width else el.text.replace("\n", " ")
        return [cmd + field_data(text)]
    if isinstance(el, Code128Element):
        # Spell out the subsets so the printer matches the preview. Data that can't be
        # encoded that way goes as it is ('>' is the invocation character, so '>0').
        data = barcode.code128_zpl_data(el.data) or el.data.replace(">", ">0")
        own_text = el.show_text and not el.text_size
        line = "Y" if own_text else "N"
        fo = field_origin(el, (0, 0, el.size()[0], el.height))       # the bars
        out = [f"^BY{el.module}{fo}^BC{o},{el.height},{line},N,N,N" + field_data(data)]
        if el.show_text and el.text_size:
            out.append(barcode_text_zpl(el))
        return out
    if isinstance(el, QRElement):
        # Printers draw ^BQ about 10 dots below its field origin; compensate so the
        # code lands where the designer shows it.
        fo = f"^FO{max(0, el.x)},{max(0, el.y - QR_Y_SHIFT)}"
        return [f"{fo}^BQN,2,{el.magnification}" + field_data(f"{el.error_correction}A,{el.data}")]
    if isinstance(el, BarcodeElement):
        sym = symbology.get(el.symbology)
        if sym is None:
            return []
        by = f"^BY{el.module},{el.ratio:.1f}" if sym.two_width else f"^BY{el.module}"
        cmd = sym.zpl(el)
        if el.rotation % 360 and el.can_rotate():
            cmd = cmd.replace(f"^{sym.command}N", f"^{sym.command}{o}", 1)
            if sym.shape in ("linear", "postal"):
                fo = field_origin(el, (0, 0, el.size()[0], el.height))   # the bars
        return [f"{by}{fo}{cmd}" + field_data(sym.zpl_data(el.data, el))]
    if isinstance(el, BoxElement):
        t = min(el.w, el.h) if el.filled else max(1, min(el.thickness, el.w, el.h))
        return [f"{fo}^GB{el.w},{el.h},{t},B,{el.rounding}^FS"]
    if isinstance(el, EllipseElement):
        t = min(el.w, el.h) if el.filled else max(1, el.thickness)
        return [f"{fo}^GE{el.w},{el.h},{t},B^FS"]
    if isinstance(el, ImageElement):
        gf = image_to_gf(el)
        return [f"{fo}{gf}^FS"] if gf else []
    return []


def _needs_raster(el) -> bool:
    """Non-ASCII text goes as a graphic: printer ZPL emulations (Xprinter etc.)
    ignore ^CI28 and print nothing for Cyrillic."""
    return isinstance(el, TextElement) and not el.text.isascii()


def placement(el, label: Label) -> str:
    """'inside', 'cut' (partly outside the label) or 'outside'."""
    bx, by, bw, bh = el.bounds()
    W, H = label.width_dots, label.height_dots
    if bx >= W or by >= H or bx + bw <= 0 or by + bh <= 0:
        return "outside"
    if bx < 0 or by < 0 or bx + bw > W or by + bh > H:
        return "cut"
    return "inside"


def visible_zpl(el, label: Label) -> List[str]:
    """ZPL for an element hanging off the left or top edge: printers can't place a field
    at a negative position, so send the part inside the label as a graphic."""
    from . import render
    bitmap = render.visible_bitmap(el, label)
    if bitmap is None:
        return []
    x, y, row_bytes, rows, data = bitmap
    total = row_bytes * rows
    fr = "^FR" if getattr(el, "reverse", False) else ""
    return [f"^FO{x},{y}{fr}^GFA,{total},{total},{row_bytes},{data.hex().upper()}^FS"]


def to_print(lab: Label) -> Label:
    """The label as the printer sees it: with an orientation, the design is turned onto
    the label stock, every element moving and turning with it, and width and height
    swap at 90 and 270 degrees. lab is changed in place (it is the filled copy)."""
    if lab.orientation % 360:
        lab.turn_content(lab.orientation)
        lab.orientation = 0
    return lab


def label_zpl(label: Label, values: Optional[Dict[str, str]] = None,
              copies: Optional[int] = None, offset: int = 0) -> str:
    """ZPL for the label. offset: how many labels have been printed before this one in
    the job, for {counter}."""
    n = copies if copies is not None else label.copies
    if n > 1 and label.uses_counter():
        # each copy gets its own number, so each is its own label
        return "".join(label_zpl(label, values, 1, offset + i) for i in range(n))
    lab = label.filled(values, offset)
    if lab.orientation % 360 or any(el.x < 0 or el.y < 0 or el.rotation % 360
                                    or _needs_raster(el) for el in lab.elements):
        # sizes decide what is left on the label, and where rotated fields go
        from . import render
        render.measure(lab)
    lab = to_print(lab)
    lines = [
        "^XA",
        "^CI28",                      # field data is UTF-8
        f"^PW{lab.width_dots}",
        f"^LL{lab.height_dots}",
        "^LH0,0",
    ]
    if lab.darkness:
        lines.append(f"^MD{max(-30, min(30, lab.darkness))}")
    for el in lab.elements:
        # Only what is inside the label prints. Past the right or bottom edge the
        # printer cuts fields off itself; past the left or top it can't place them.
        if el.x >= lab.width_dots or el.y >= lab.height_dots:
            continue
        if el.x < 0 or el.y < 0 or _needs_raster(el):
            lines.extend(visible_zpl(el, lab))
        else:
            lines.extend(element_zpl(el))
    if n > 1:
        lines.append(f"^PQ{n}")
    lines.append("^XZ")
    return "\n".join(lines) + "\n"


def batch_zpl(label: Label, rows: Iterable[Dict[str, str]]) -> str:
    """One label per row of variable values, each printed `label.copies` times; the
    counter goes on from label to label."""
    rows = list(rows)
    per_row = label.copies if label.uses_counter() else 1
    return "".join(label_zpl(label, row, offset=i * per_row) for i, row in enumerate(rows))


def labels_printed(label: Label, rows: int = 1) -> int:
    """How many labels a job prints, for moving the counter on."""
    return rows * max(1, label.copies)
