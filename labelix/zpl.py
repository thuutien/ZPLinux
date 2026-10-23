"""Turn a Label into ZPL II."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .model import (BoxElement, Code128Element, EllipseElement, ImageElement, Label,
                    QRElement, TextElement)


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


def element_zpl(el) -> List[str]:
    fo = f"^FO{max(0, el.x)},{max(0, el.y)}"
    if isinstance(el, TextElement):
        cmd = f"{fo}^A0N,{el.height},{el.width or el.height}"
        if el.block_width:
            cmd += f"^FB{el.block_width},{max(1, el.max_lines)},0,{el.align},0"
        if el.reverse:
            cmd += "^FR"
        # Without a text block the printer can't break lines, so keep it on one line.
        text = block_text(el.text) if el.block_width else el.text.replace("\n", " ")
        return [cmd + field_data(text)]
    if isinstance(el, Code128Element):
        # Mode N starts in subset B; '>' is the invocation character, so a literal one is '>0'.
        data = el.data.replace(">", ">0")
        line = "Y" if el.show_text else "N"
        return [f"^BY{el.module}{fo}^BCN,{el.height},{line},N,N,N" + field_data(data)]
    if isinstance(el, QRElement):
        # Printers draw ^BQ about 10 dots below its field origin; compensate so the
        # code lands where the designer shows it.
        fo = f"^FO{max(0, el.x)},{max(0, el.y - QR_Y_SHIFT)}"
        return [f"{fo}^BQN,2,{el.magnification}" + field_data(f"{el.error_correction}A,{el.data}")]
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


def label_zpl(label: Label, values: Optional[Dict[str, str]] = None,
              copies: Optional[int] = None) -> str:
    lab = label.filled(values)
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
        lines.extend(element_zpl(el))
    n = copies if copies is not None else lab.copies
    if n > 1:
        lines.append(f"^PQ{n}")
    lines.append("^XZ")
    return "\n".join(lines) + "\n"


def batch_zpl(label: Label, rows: Iterable[Dict[str, str]]) -> str:
    """One label per row of variable values, each printed `label.copies` times."""
    return "".join(label_zpl(label, row) for row in rows)
