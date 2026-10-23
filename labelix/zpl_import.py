"""Read ZPL II (for example a ZebraDesigner "print to file" .prn) back into a Label.

Only what Labelix can edit comes back as elements: font 0 text, Code 128 and QR
codes, boxes, lines, ellipses and graphics. Everything else is reported in
`ImportResult.warnings` so the user knows what changed.
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import barcode
from .model import (BoxElement, Code128Element, EllipseElement, ImageElement, Label,
                    QRElement, TextElement, dpmm)
from .zpl import QR_Y_SHIFT

# Where things land relative to ^FT, measured against Labelary at 203 dpi:
# text: the baseline is 3/4 of the character height (rounded down) below the top of the cell;
# Code 128: ^FT is the bottom of the bars; QR: the bottom is 3 modules above ^FT.
FT_BASELINE = 0.75
FT_QR_MODULES = 3

# Printer set-up and other commands that don't affect what a label looks like.
IGNORED = {
    "CI", "LT", "LS", "LR", "MM", "MN", "MT", "MU", "PR", "PM", "PO", "PF", "JM", "JU", "JS",
    "JZ", "JB", "JC", "JD", "JE", "JG", "JL", "JN", "JO", "JP", "JR", "JW", "SD", "TA", "CT",
    "CD", "CO", "FX", "FN", "FZ", "KD", "KL", "KN", "KP", "MF", "ML", "MP", "MW", "NC", "NI",
    "NS", "PA", "PH", "PL", "PN", "PP", "PS", "RS", "SC", "SE", "SF", "SL", "SO", "SP",
    "SQ", "SS", "SX", "SZ", "WD", "XB", "XF", "DF", "CW", "CM", "EG", "HB", "HD", "HF",
    "HG", "HH", "HI", "HM", "HQ", "HS", "HT", "HU", "HV", "HW", "HZ", "ID", "IL", "IS",
    "FL", "FM", "FP", "LF",
}
BARCODE_NAMES = {
    "B0": "Aztec", "B1": "Code 11", "B2": "Interleaved 2 of 5", "B3": "Code 39", "B4": "Code 49",
    "B5": "Planet", "B7": "PDF417", "B8": "EAN-8", "B9": "UPC-E", "BA": "Code 93",
    "BB": "Codablock", "BD": "MaxiCode", "BE": "EAN-13", "BF": "MicroPDF417",
    "BI": "Industrial 2 of 5", "BJ": "Standard 2 of 5", "BK": "ANSI Codabar", "BL": "LOGMARS",
    "BM": "MSI", "BO": "Aztec", "BP": "Plessey", "BR": "GS1 DataBar", "BS": "UPC/EAN extension",
    "BT": "TLC39", "BU": "UPC-A", "BX": "Data Matrix", "BZ": "POSTAL",
}
# 2D codes come in as QR codes, the rest as Code 128.
TWO_D = {"B0", "B7", "BB", "BD", "BF", "BO", "BX"}
# Which ^Bx parameter is the bar height (after orientation); most have it first.
HEIGHT_PARAM = {"B1": 2, "B3": 2, "BK": 2}


@dataclass
class ImportResult:
    label: Label
    values: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class ZplImportError(ValueError):
    pass


# ---- graphics ---------------------------------------------------------------

_ACS_COUNTS = {**{chr(ord("G") + i): i + 1 for i in range(19)},
               **{chr(ord("g") + i): (i + 1) * 20 for i in range(20)}}


def _expand_ascii(data: str, row_bytes: int) -> bytes:
    """Hex graphic data, with Zebra's ASCII compression (G-Y/g-z repeat counts,
    ',' fills the row with 0, '!' with 1, ':' repeats the previous row)."""
    row_chars = row_bytes * 2
    rows: List[str] = []
    cur: List[str] = []
    count = 0

    def finish(fill: str = "0"):
        line = "".join(cur)
        line = (line + fill * row_chars)[:row_chars] if len(line) < row_chars else line
        rows.append(line)
        cur.clear()

    for c in data:
        if c in _ACS_COUNTS:
            count += _ACS_COUNTS[c]
        elif c in "0123456789ABCDEFabcdef":
            cur.append(c * (count or 1))
            count = 0
            while sum(len(s) for s in cur) >= row_chars:
                joined = "".join(cur)
                rows.append(joined[:row_chars])
                cur[:] = [joined[row_chars:]] if len(joined) > row_chars else []
        elif c == ",":
            finish("0")
        elif c == "!":
            finish("F")
        elif c == ":":
            if cur:
                finish("0")
            rows.append(rows[-1] if rows else "0" * row_chars)
        # whitespace and anything else is ignored
    if cur and "".join(cur):
        finish("0")
    return binascii.unhexlify("".join(rows))


def decode_graphic(data: str, row_bytes: int) -> bytes:
    """Raw 1-bit rows from ^GF / ~DG data in any of Zebra's encodings."""
    data = data.strip()
    if data.startswith((":Z64:", ":B64:")):
        body = data[5:].split(":", 1)[0]
        raw = base64.b64decode(body + "=" * (-len(body) % 4))
        return zlib.decompress(raw) if data.startswith(":Z64:") else raw
    return _expand_ascii(data, row_bytes)


def bitmap_png(bits: bytes, row_bytes: int, height: int) -> bytes:
    """A black and white PNG from 1-bit rows where a set bit prints black."""
    width = row_bytes * 8
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        # PNG grayscale 1-bit: 1 is white, so invert the printer's bits.
        raw.extend(b ^ 0xFF for b in bits[y * row_bytes:(y + 1) * row_bytes])

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# ---- tokenizer --------------------------------------------------------------

def _commands(text: str, caret: str = "^", tilde: str = "~"):
    """(prefix, name, params) for each command. A name is two characters, except
    ^A (font: the next character names the font) and ^A@."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c not in (caret, tilde):
            i += 1
            continue
        j = i + 1
        while j < n and text[j] not in (caret, tilde):
            j += 1
        body = text[i + 1:j]
        i = j
        if not body:
            continue
        if body[0] in "Aa" and c == caret:
            yield c, "A", body[1:]
            continue
        yield c, body[:2].upper(), body[2:]


def _ints(params: str, count: int) -> List[Optional[int]]:
    out: List[Optional[int]] = []
    for p in (params.split(",") + [""] * count)[:count]:
        p = p.strip()
        try:
            out.append(int(float(p)) if p else None)
        except ValueError:
            out.append(None)
    return out


def _unhex(data: str, indicator: str) -> str:
    """Field data written with ^FH: indicator followed by two hex digits is a byte."""
    out = bytearray()
    i = 0
    raw = data.encode("utf-8")
    ind = indicator.encode("utf-8")[0]
    while i < len(raw):
        if raw[i] == ind and re.fullmatch(rb"[0-9A-Fa-f]{2}", raw[i + 1:i + 3]):
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode("utf-8", errors="replace")


def _code128_data(data: str) -> str:
    """Drop Code 128 invocation codes (>: start B, >; start C, >5 switch to C ...);
    >0 is a literal '>'."""
    return re.sub(r">(.)", lambda m: ">" if m.group(1) == "0" else "", data, flags=re.S)


def _qr_data(data: str) -> Tuple[str, str]:
    """(error correction, data) from ^BQ field data such as 'QA,https://...'."""
    head, sep, rest = data.partition(",")
    if not sep:
        return "Q", data
    ec = head[:1].upper() if head[:1].upper() in "HQML" else "M"
    mode = head[1:2].upper()
    if mode == "M" and rest:
        kind, rest = rest[0].upper(), rest[1:]
        if kind == "B" and len(rest) >= 4 and rest[:4].isdigit():
            rest = rest[4:]
    return ec, rest


# ---- parser -----------------------------------------------------------------

@dataclass
class _Field:
    x: int = 0
    y: int = 0
    typeset: bool = False        # ^FT rather than ^FO
    font: str = "0"
    height: int = 0
    width: int = 0
    rotation: str = "N"
    block: Optional[Tuple[int, int, str]] = None   # width, max lines, alignment
    reverse: bool = False
    hex_indicator: Optional[str] = None
    kind: Optional[str] = None   # "BC", "BQ", "GB", "GE", "GC", "GD", "GF", "XG", other barcode
    params: str = ""
    data: Optional[str] = None
    number: Optional[int] = None  # ^FN


def _split_labels(text: str) -> List[str]:
    parts = re.split(r"\^XZ", text, flags=re.I)
    labels = []
    for p in parts:
        k = re.search(r"\^XA", p, flags=re.I)
        if k:
            labels.append(p[k.start():])
    return labels


def parse(text: str, dpi: Optional[int] = None) -> ImportResult:
    """Turn ZPL into a Label. Raises ZplImportError when there is nothing to import."""
    graphics: Dict[str, Tuple[bytes, int, int]] = {}
    # ~DG downloads may sit in their own block before the label that uses them.
    for _p, name, params in _commands(text):
        if _p == "~" and name == "DG":
            m = re.match(r"\s*([^,]+),\s*(\d+),\s*(\d+),(.*)", params, re.S)
            if m:
                total, row_bytes = int(m.group(2)), int(m.group(3))
                try:
                    bits = decode_graphic(m.group(4), row_bytes)[:total]
                    graphics[_graphic_name(m.group(1))] = (bits, row_bytes, total // max(1, row_bytes))
                except (ValueError, binascii.Error, zlib.error):
                    pass

    blocks = [b for b in _split_labels(text) if re.search(r"\^F[DVOT]", b, re.I)]
    if not blocks:
        raise ZplImportError("No label found. A ZPL label starts with ^XA and ends with ^XZ.")
    result = _parse_label(blocks[0], dpi, graphics)
    if len(blocks) > 1:
        result.warnings.append(f"The file holds {len(blocks)} labels; only the first was imported.")
    return result


def _graphic_name(name: str) -> str:
    name = name.strip().upper()
    if ":" not in name:
        name = "R:" + name
    if "." not in name.split(":", 1)[1]:
        name += ".GRF"
    return name


def _parse_label(text: str, dpi: Optional[int], graphics) -> ImportResult:
    warnings: List[str] = []
    notes = set()

    def note(msg: str):
        if msg not in notes:
            notes.add(msg)
            warnings.append(msg)

    width = length = None
    home_x = home_y = 0
    darkness = 0
    copies = 1
    default_font = ("0", 0, 0)    # ^CF
    default_rotation = "N"         # ^FW
    module, bar_height = 2, 10     # ^BY
    fields: List[_Field] = []
    f = _Field()
    ignored = set()
    caret, tilde = "^", "~"

    for prefix, name, params in _commands(text, caret, tilde):
        if prefix == "~":
            if name == "DG":
                continue
            if name not in ("TA", "SD", "JS", "JC", "JD", "JE", "JL", "JN", "JO", "JP", "JR", "RO"):
                ignored.add("~" + name)
            continue
        if name == "XA" or name == "XZ":
            continue
        if name == "PW":
            width = _ints(params, 1)[0]
        elif name == "LL":
            length = _ints(params, 1)[0]
        elif name == "LH":
            home_x, home_y = [v or 0 for v in _ints(params, 2)]
        elif name == "MD":
            v = params.strip()
            try:
                darkness = max(-30, min(30, round(float(v))))
            except ValueError:
                pass
        elif name == "PQ":
            copies = max(1, _ints(params, 1)[0] or 1)
        elif name == "CF":
            p = params.split(",")
            h, w = _ints(",".join(p[1:]), 2) if len(p) > 1 else (None, None)
            default_font = (p[0][:1] or "0", h or default_font[1], w or 0)
        elif name == "FW":
            default_rotation = (params[:1] or "N").upper()
        elif name == "BY":
            w, _r, h = _ints(params, 3)
            module = w or module
            bar_height = h or bar_height
        elif name in ("FO", "FT"):
            x, y = _ints(params, 2)
            f.x, f.y, f.typeset = (x or 0) + home_x, (y or 0) + home_y, name == "FT"
        elif name == "A":
            font = params[:1] or "0"
            rest = params[1:]
            if font == "@":
                p = rest.split(",")
                rot = p[0][:1] if p and p[0] else ""
                h, w = _ints(",".join(p[1:3]), 2)
                note("TrueType fonts (^A@) were replaced by font 0 at the same size.")
            else:
                rot = rest[:1] if rest[:1].isalpha() else ""
                h, w = _ints(rest[1:].lstrip(",") if rot else rest.lstrip(","), 2)
                if font != "0":
                    note(f"Printer font {font} was replaced by font 0 at the same size.")
            f.font = font
            f.rotation = (rot or default_rotation).upper()
            f.height = h or 0
            f.width = w or 0
        elif name == "FB":
            p = params.split(",")
            w, lines = _ints(params, 2)
            align = (p[3].strip()[:1].upper() if len(p) > 3 and p[3].strip() else "L")
            f.block = (w or 0, lines or 1, align if align in "LCRJ" else "L")
        elif name == "TB":
            p = params.split(",")
            _o, w, h = (p + ["", "", ""])[:3]
            f.block = (_ints(w, 1)[0] or 0, 0, "L")
            f.params = h
        elif name == "FR":
            f.reverse = True
        elif name == "FH":
            f.hex_indicator = params[:1] or "_"
        elif name in ("FD", "FV"):
            f.data = params
        elif name == "SN":
            f.data = params.split(",")[0]
            note("Serial numbers (^SN) were imported as plain text.")
        elif name == "FN":
            f.number = _ints(params, 1)[0]
        elif name in ("BC", "BQ", "GB", "GE", "GC", "GD") or name in BARCODE_NAMES:
            f.kind, f.params = name, params
        elif name == "GF":
            f.kind, f.params = "GF", params
        elif name == "XG":
            f.kind, f.params = "XG", params
        elif name == "FS":
            if f.kind or f.data is not None:
                fields.append(f)
            f = _Field()
        elif name == "CC":
            caret = params[:1] or caret
            note("A changed command prefix (^CC) is not supported; the rest may be missing.")
        elif name in IGNORED or name in ("FX",):
            pass
        else:
            ignored.add("^" + name)

    if f.kind or f.data is not None:
        fields.append(f)

    if dpi is None:
        # A GK420d and most desktop printers are 203 dpi; wider than 4.1 in at 203 dpi
        # means a higher resolution.
        dpi = 203 if not width or width <= 832 else 300 if width <= 1248 else 600
    d = dpmm(dpi)

    label = Label(dpi=dpi, darkness=darkness, copies=copies)
    values: Dict[str, str] = {}
    for fld in fields:
        el = _element(fld, default_font, module, bar_height, graphics, values, note)
        if el is not None:
            label.elements.append(el)

    if not label.elements:
        raise ZplImportError("The label has nothing Labelix can show (no text, codes, "
                             "boxes or graphics).")

    bottom = max(_bottom(e) for e in label.elements)
    right = max(_right(e) for e in label.elements)
    label.width_mm = round((width or right + 10) / d, 2)
    label.height_mm = round((length or bottom + 10) / d, 2)
    if ignored:
        warnings.append("Ignored: " + ", ".join(sorted(ignored)))
    warnings.insert(0, f"Assumed {dpi} dpi (change it on the Label tab if your printer differs).")
    return ImportResult(label, values, warnings)


def _bottom(el) -> int:
    return el.bounds()[1] + el.bounds()[3]


def _right(el) -> int:
    return el.bounds()[0] + el.bounds()[2]


def _element(f: _Field, default_font, module, bar_height, graphics, values, note):
    data = f.data or ""
    if f.hex_indicator:
        data = _unhex(data, f.hex_indicator)
    if f.number is not None:
        var = f"field{f.number}"
        values[var] = data
        data = "{" + var + "}"
        note("Numbered fields (^FN) became variables named field1, field2 ...")
    if f.rotation not in ("N", "") and f.kind is None:
        note("Rotated text was imported upright.")

    if f.kind is None:
        h = f.height or default_font[1] or 30
        w = f.width or (default_font[2] if not f.height else 0)
        el = TextElement(x=f.x, y=f.y, height=h, width=0 if w == h else w,
                         reverse=f.reverse)
        if f.block:
            bw, lines, align = f.block
            if not lines:      # ^TB: lines from the block height
                lines = max(1, (_ints(f.params, 1)[0] or h) // h)
            el.block_width, el.max_lines, el.align = bw, lines, align
            el.text = data.replace("\\&", "\n").replace("\\\\", "\\")
        else:
            el.text = data
        if f.typeset:
            lines = el.max_lines if el.block_width else 1
            el.y = f.y - (lines - 1) * h - int(FT_BASELINE * h)
        el.y = max(0, el.y)
        return el

    if f.kind == "BC":
        p = f.params.split(",")
        rot = (p[0][:1] or "N").upper()
        h = _ints(",".join(p[1:2]), 1)[0] or bar_height
        line = (p[2].strip().upper() if len(p) > 2 and p[2].strip() else "Y") == "Y"
        if len(p) > 3 and p[3].strip().upper() == "Y":
            note("Text above Code 128 barcodes was moved below them.")
        if rot != "N":
            note("Rotated barcodes were imported upright.")
        el = Code128Element(x=f.x, y=f.y, data=_code128_data(data), height=h,
                            module=module, show_text=line)
        if f.typeset:
            el.y = max(0, f.y - h)
        return el

    if f.kind == "BQ":
        p = _ints(f.params, 3)
        mag = max(1, min(10, p[2] or 2))
        ec, qdata = _qr_data(data)
        el = QRElement(x=f.x, y=f.y + QR_Y_SHIFT, data=qdata, magnification=mag,
                       error_correction=ec)
        if f.typeset:
            m = barcode.qr_matrix(qdata, ec)
            size = (len(m) if m else 25) * mag
            el.y = f.y - FT_QR_MODULES * mag - size
        el.y = max(0, el.y)
        return el

    if f.kind == "GB":
        w, h, t, _c, r = _ints(f.params, 5)
        color = (f.params.split(",") + [""] * 4)[3].strip().upper()
        t = t or 1
        w, h = max(w or 1, t), max(h or 1, t)
        if color == "W" and not f.reverse:
            note("White boxes were imported as black outlines.")
        # A stroke that covers the whole box makes a solid box; a thin one stays a line.
        el = BoxElement(x=f.x, y=f.y, w=w, h=h, thickness=min(t, w, h),
                        rounding=max(0, min(8, r or 0)),
                        filled=t * 2 >= min(w, h) and min(w, h) > 8)
        if f.typeset:
            el.y = max(0, f.y - h)
        return el

    if f.kind in ("GE", "GC"):
        if f.kind == "GC":
            dia, t = _ints(f.params, 2)
            w = h = dia or 3
        else:
            w, h, t = _ints(f.params, 3)
            w, h = w or 3, h or 3
        t = t or 1
        el = EllipseElement(x=f.x, y=f.y, w=max(3, w), h=max(3, h), thickness=t,
                            filled=t >= min(w, h) // 2)
        if f.typeset:
            el.y = max(0, f.y - h)
        return el

    if f.kind == "GD":
        note("Diagonal lines (^GD) are not supported and were left out.")
        return None

    if f.kind in ("GF", "XG"):
        if f.kind == "GF":
            m = re.match(r"\s*([ABCabc])?,?\s*(\d+)?,\s*(\d+),\s*(\d+),(.*)", f.params, re.S)
            if not m:
                note("A graphic (^GF) could not be read and was left out.")
                return None
            fmt = (m.group(1) or "A").upper()
            total, row_bytes = int(m.group(3)), int(m.group(4))
            if fmt != "A":
                note("Binary graphics (^GFB/^GFC) are not supported and were left out.")
                return None
            try:
                bits = decode_graphic(m.group(5), row_bytes)[:total]
            except (ValueError, binascii.Error, zlib.error):
                note("A graphic (^GF) could not be read and was left out.")
                return None
            height = total // max(1, row_bytes)
            mx = my = 1
        else:
            p = f.params.split(",")
            g = graphics.get(_graphic_name(p[0]))
            if g is None:
                note("A stored graphic (^XG) was not in the file and was left out.")
                return None
            bits, row_bytes, height = g
            mx, my = [v or 1 for v in _ints(",".join(p[1:]), 2)]
        bits = bits.ljust(row_bytes * height, b"\0")
        png = bitmap_png(bits, row_bytes, height)
        el = ImageElement(x=f.x, y=f.y, png_b64=base64.b64encode(png).decode("ascii"),
                          w=row_bytes * 8 * mx, h=height * my, threshold=128, keep_aspect=True)
        if f.typeset:
            el.y = max(0, f.y - el.h)
        return el

    if f.kind in TWO_D:
        note(f"{BARCODE_NAMES[f.kind]} codes became QR codes; Labelix makes Code 128 "
             "and QR codes only.")
        el = QRElement(x=f.x, y=f.y, data=data, magnification=max(1, min(10, module * 2)))
        if f.typeset:
            el.y = max(0, f.y - 25 * el.magnification)
        return el

    if f.kind in BARCODE_NAMES:
        note(f"{BARCODE_NAMES[f.kind]} barcodes became Code 128; Labelix makes Code 128 "
             "and QR codes only.")
        i = HEIGHT_PARAM.get(f.kind, 1)
        h = _ints(",".join(f.params.split(",")[i:i + 1]), 1)[0] or bar_height
        el = Code128Element(x=f.x, y=f.y, data=data, height=h, module=module)
        if f.typeset:
            el.y = max(0, f.y - h)
        return el
    return None
