"""Label document model. All positions and sizes are in printer dots."""

from __future__ import annotations

import base64
import copy
import datetime
import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional

MM_PER_INCH = 25.4
DPI_CHOICES = (203, 300, 600)
VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def dpmm(dpi: int) -> int:
    """Dots per millimetre as Zebra names them (203 dpi -> 8, 300 -> 12, 600 -> 24)."""
    return {203: 8, 300: 12, 600: 24}.get(dpi, round(dpi / MM_PER_INCH))


# unit id -> (label, decimals shown)
UNITS = {"mm": ("mm", 1), "in": ("in", 3), "dots": ("dots", 0)}


def to_unit(dots: float, unit: str, dpi: int) -> float:
    if unit == "mm":
        return dots / dpmm(dpi)
    if unit == "in":
        return dots / dpmm(dpi) / MM_PER_INCH
    return dots


def from_unit(value: float, unit: str, dpi: int) -> int:
    if unit == "mm":
        return round(value * dpmm(dpi))
    if unit == "in":
        return round(value * MM_PER_INCH * dpmm(dpi))
    return round(value)


def interpretation_height(module: int) -> int:
    """Approximate height of the text line printed under a Code 128 barcode."""
    return 10 + 8 * module


def rotate_rect(rect, w: float, h: float, r: int) -> tuple:
    """Where rect (x, y, w, h) inside a w x h area ends up when the area is turned r
    degrees clockwise (r = 0, 90, 180, 270) about its top-left, in the turned area."""
    x, y, rw, rh = rect
    r %= 360
    if r == 90:
        return (h - y - rh, x, rh, rw)
    if r == 180:
        return (w - x - rw, h - y - rh, rw, rh)
    if r == 270:
        return (y, w - x - rw, rh, rw)
    return (x, y, rw, rh)


@dataclass
class Element:
    kind = "element"
    x: int = 20               # top-left of the (rotated) element
    y: int = 20
    rotation: int = 0         # clockwise: 0, 90, 180 or 270
    locked: bool = False      # can't be clicked, dragged or resized on the canvas

    def bounds(self) -> tuple:
        """(x, y, w, h) in dots, used for selection and hit-testing: the rotated box."""
        x, y, w, h = self._box()
        return (x, y, h, w) if self.rotation % 180 == 90 else (x, y, w, h)

    def size(self) -> tuple:
        """(w, h) before rotation."""
        return self._box()[2:]

    def resize(self, w: int, h: int) -> None:
        """Resize to a w x h box on the label (after rotation)."""
        if self.rotation % 180 == 90:
            w, h = h, w
        self._resize(w, h)

    def can_rotate(self) -> bool:
        return True

    def _box(self) -> tuple:
        return (self.x, self.y, 10, 10)

    def _resize(self, w: int, h: int) -> None:
        pass

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind
        return d

    def text_fields(self) -> List[str]:
        """Names of the fields that may hold {variables}."""
        return []


@dataclass(frozen=True)
class BitmapFont:
    """One of the fonts built into Zebra printers (203 dpi), measured on Labelary."""
    name: str
    cell_h: int                # character cell at 1x, in dots
    cell_w: int
    gap: int                   # space between characters
    cap_h: int                 # how tall a capital H is drawn, and where in the cell
    ink_w: int
    x_off: int = 0
    y_off: int = 0
    capitals: bool = False     # lower case prints as capitals
    bold: bool = False


BITMAP_FONTS = {
    "A": BitmapFont("A", 9, 5, 1, 7, 5),
    "B": BitmapFont("B", 11, 7, 2, 11, 7, capitals=True, bold=True),
    "C": BitmapFont("C", 18, 10, 2, 14, 9),
    "D": BitmapFont("D", 18, 10, 2, 14, 9),
    "E": BitmapFont("E (OCR-B)", 28, 15, 5, 20, 12, 1, 3),
    "F": BitmapFont("F", 26, 13, 3, 21, 13),
    "G": BitmapFont("G", 60, 40, 8, 48, 31, 4, 0),
    "H": BitmapFont("H (OCR-A)", 21, 13, 6, 21, 13, capitals=True),
}


def magnification(size: int, cell: int) -> int:
    """Printers draw built-in fonts at whole multiples of the cell, rounding halves up."""
    return max(1, min(10, int(size / cell + 0.5)))


@dataclass
class TextElement(Element):
    kind = "text"
    text: str = "Text"
    height: int = 40          # character height in dots
    width: int = 0            # character width in dots, 0 = same as height
    block_width: int = 0      # 0 = no text block; otherwise wrap/align inside this width
    max_lines: int = 1
    align: str = "L"          # L, C, R, J (only used with a text block)
    reverse: bool = False     # white on black (^FR)
    font: str = "0"           # 0 (scalable) or a printer font A-H
    # Filled in by the renderer so hit-testing matches what is drawn.
    measured_w: int = field(default=0, compare=False)
    measured_h: int = field(default=0, compare=False)

    def _box(self):
        w = self.block_width or self.measured_w or max(10, len(self.text) * self.height // 2)
        h = self.measured_h or self.height * max(1, self.max_lines if self.block_width else 1)
        return (self.x, self.y, w, h)

    def _resize(self, w, h):
        if self.block_width:
            self.block_width = max(10, w)
        else:
            self.height = max(10, h)

    def text_fields(self):
        return ["text"]

    def bitmap_font(self) -> Optional[BitmapFont]:
        return BITMAP_FONTS.get(self.font)

    def font_size(self) -> tuple:
        """(height, width) in dots as printed: for printer fonts, whole multiples of the
        cell; a width of 0 keeps the font's proportions."""
        bf = self.bitmap_font()
        if bf is None:
            return self.height, self.width or self.height
        mh = magnification(self.height, bf.cell_h)
        mw = magnification(self.width, bf.cell_w) if self.width else mh
        return bf.cell_h * mh, bf.cell_w * mw


@dataclass
class Code128Element(Element):
    kind = "code128"
    data: str = "123456789"
    height: int = 80
    module: int = 2           # narrow bar width in dots (^BY)
    show_text: bool = True
    text_size: int = 0        # 0: the printer's own text; else a font 0 line of this height
    measured_w: int = field(default=0, compare=False)

    def _box(self):
        w = self.measured_w or 200
        return (self.x, self.y, w, self.height + self._text_space())

    def text_gap(self) -> int:
        """Space between the bars and the text below them."""
        return max(2, self.module)

    def _text_space(self) -> int:
        if not self.show_text:
            return 0
        return (self.text_size or interpretation_height(self.module)) + self.text_gap()

    def _resize(self, w, h):
        self.height = max(10, h - self._text_space())

    def text_fields(self):
        return ["data"]


@dataclass
class QRElement(Element):
    kind = "qr"
    data: str = "https://example.com"
    magnification: int = 4    # dots per module, 1-10
    error_correction: str = "Q"  # H, Q, M, L
    measured_w: int = field(default=0, compare=False)

    def _box(self):
        s = self.measured_w or 25 * self.magnification
        return (self.x, self.y, s, s)

    def _resize(self, w, h):
        modules = max(21, (self.measured_w or 25 * self.magnification) // self.magnification)
        self.magnification = max(1, min(10, round(max(w, h) / modules)))

    def can_rotate(self):
        return False          # printers draw QR codes upright; they scan from any side

    def text_fields(self):
        return ["data"]


@dataclass
class BarcodeElement(Element):
    """Every barcode type other than Code 128 and QR; `symbology` is a key from
    symbology.SYMBOLOGIES and decides which of the settings below are used."""
    kind = "barcode"
    symbology: str = "code39"
    data: str = "ABC-123"
    height: int = 80          # bar height in dots (row height for stacked codes)
    module: int = 2           # narrow bar or module size in dots (^BY)
    ratio: float = 3.0        # wide to narrow bar ratio, 2.0-3.0 (^BY)
    show_text: bool = True
    check: bool = False       # add an optional check digit
    columns: int = 0          # 2D/stacked size, 0 = automatic
    rows: int = 0
    security: int = 0         # PDF417 error correction level
    mode: int = 0             # GS1 DataBar type, MaxiCode mode
    measured_w: int = field(default=0, compare=False)
    measured_h: int = field(default=0, compare=False)

    def _box(self):
        return (self.x, self.y, self.measured_w or 200, self.measured_h or self.height)

    def _resize(self, w, h):
        self.height = max(10, h - (self.measured_h - self.height if self.measured_h else 0))

    def text_fields(self):
        return ["data"]

    def can_rotate(self):
        return self.symbology != "maxicode"   # MaxiCode has no orientation in ZPL


@dataclass
class BoxElement(Element):
    kind = "box"
    w: int = 200
    h: int = 100
    thickness: int = 3
    rounding: int = 0         # 0-8
    filled: bool = False

    def _box(self):
        return (self.x, self.y, self.w, self.h)

    def _resize(self, w, h):
        self.w = max(1, w)
        self.h = max(1, h)


@dataclass
class EllipseElement(Element):
    kind = "ellipse"
    w: int = 150
    h: int = 100
    thickness: int = 3
    filled: bool = False

    def _box(self):
        return (self.x, self.y, self.w, self.h)

    def _resize(self, w, h):
        self.w = max(3, w)
        self.h = max(3, h)


@dataclass
class ImageElement(Element):
    kind = "image"
    png_b64: str = ""         # original image file, base64, so designs stay portable
    w: int = 150
    h: int = 150
    threshold: int = 128      # pixels darker than this print black
    keep_aspect: bool = True

    def _box(self):
        return (self.x, self.y, self.w, self.h)

    def _resize(self, w, h):
        if self.keep_aspect and self.w and self.h:
            ratio = self.h / self.w
            self.w = max(4, w)
            self.h = max(4, round(self.w * ratio))
        else:
            self.w = max(4, w)
            self.h = max(4, h)

    def image_bytes(self) -> bytes:
        return base64.b64decode(self.png_b64) if self.png_b64 else b""


def turn(el: Element, r: int, force: bool = False) -> None:
    """Turn an element r degrees clockwise in place (its box stays at x, y). Boxes and
    ellipses swap width and height; QR codes and MaxiCode turn only with force (when the
    whole design turns, as they still print upright)."""
    r %= 360
    if isinstance(el, (BoxElement, EllipseElement)):
        if r % 180:
            el.w, el.h = el.h, el.w
    elif el.can_rotate() or force:
        el.rotation = (el.rotation + r) % 360


def describe(el: Element) -> str:
    """Short human description for the layers list."""
    if isinstance(el, TextElement):
        return el.text.replace("\n", " ").strip() or "(empty text)"
    if isinstance(el, (Code128Element, QRElement, BarcodeElement)):
        return el.data or "(no data)"
    if isinstance(el, (BoxElement, EllipseElement, ImageElement)):
        return f"{el.w} × {el.h} dots"
    return el.kind


KIND_NAMES = {"text": "Text", "code128": "Barcode", "qr": "QR code", "barcode": "Barcode",
              "box": "Box", "ellipse": "Ellipse", "image": "Image"}


KINDS: Dict[str, type] = {c.kind: c for c in (
    TextElement, Code128Element, QRElement, BarcodeElement, BoxElement, EllipseElement,
    ImageElement)}


def element_from_dict(d: dict) -> Element:
    cls = KINDS[d["kind"]]
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


# Fields filled in when printing: type {date}, {time} or {counter} in any text or barcode.
AUTO_FIELDS = ("date", "time", "counter")
# (strftime format, how it reads)
DATE_FORMATS = (("%d/%m/%Y", "31/12/2026"), ("%m/%d/%Y", "12/31/2026"),
                ("%Y-%m-%d", "2026-12-31"), ("%d.%m.%Y", "31.12.2026"),
                ("%d %b %Y", "31 Dec 2026"))
TIME_FORMATS = (("%H:%M:%S", "23:59:59"), ("%H:%M", "23:59"), ("%I:%M %p", "11:59 PM"))


@dataclass
class Label:
    width_mm: float = 101.6   # 4 x 6 inch shipping label by default
    height_mm: float = 152.4
    dpi: int = 203
    darkness: int = 0         # -30..30 relative (^MD); 0 = printer default
    copies: int = 1
    elements: List[Element] = field(default_factory=list)
    # Printing turns the design this many degrees clockwise onto the label stock, for
    # example a 6 x 4 in landscape design printed on 4 x 6 in labels. width_mm and
    # height_mm are the design's; the stock is the other way round at 90 and 270.
    orientation: int = 0
    # Automatic fields
    date_format: str = "%d/%m/%Y"
    time_format: str = "%H:%M:%S"
    counter_next: int = 1     # the number the next printed label gets
    counter_step: int = 1
    counter_digits: int = 0   # pad with zeros to this many digits; 0 = no padding

    @property
    def width_dots(self) -> int:
        return round(self.width_mm * dpmm(self.dpi))

    @property
    def height_dots(self) -> int:
        return round(self.height_mm * dpmm(self.dpi))

    @property
    def stock_mm(self) -> tuple:
        """(width, height) of the label stock the design prints on."""
        if self.orientation % 180:
            return (self.height_mm, self.width_mm)
        return (self.width_mm, self.height_mm)

    def turn_content(self, degrees: int) -> None:
        """Turn the paper and every object together by degrees clockwise. Needs the
        elements' measured sizes."""
        delta = degrees % 360
        W, H = self.width_dots, self.height_dots
        for el in self.elements:
            nx, ny, _w, _h = rotate_rect(el.bounds(), W, H, delta)
            turn(el, delta, force=True)
            el.x, el.y = round(nx), round(ny)
        if delta % 180:
            self.width_mm, self.height_mm = self.height_mm, self.width_mm

    def variables(self) -> List[str]:
        """Variable names used anywhere in the label, in order of first use."""
        seen: List[str] = []
        for el in self.elements:
            for name in el.text_fields():
                for var in VAR_RE.findall(getattr(el, name)):
                    if var not in seen:
                        seen.append(var)
        return seen

    def manual_variables(self) -> List[str]:
        """The variables the user fills in (not {date}, {time} or {counter})."""
        return [v for v in self.variables() if v not in AUTO_FIELDS]

    def uses_counter(self) -> bool:
        return "counter" in self.variables()

    def auto_values(self, offset: int = 0, now: Optional[datetime.datetime] = None) -> dict:
        """{date}, {time} and {counter} for the label printed offset labels after the
        next one."""
        now = now or datetime.datetime.now()
        n = self.counter_next + offset * self.counter_step
        return {"date": now.strftime(self.date_format), "time": now.strftime(self.time_format),
                "counter": str(n).zfill(self.counter_digits) if n >= 0 else str(n)}

    def filled(self, values: Optional[Dict[str, str]], offset: int = 0) -> "Label":
        """A copy with {variables} replaced, {date}, {time} and {counter} included (the
        counter offset labels on). Unknown variables are left as they are."""
        out = copy.deepcopy(self)
        values = {**(values or {}), **self.auto_values(offset)}
        for el in out.elements:
            for name in el.text_fields():
                setattr(el, name, VAR_RE.sub(
                    lambda m: str(values.get(m.group(1), m.group(0))), getattr(el, name)))
        return out

    def to_json(self) -> str:
        d = {
            "format": "zplinux",
            "version": 1,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "dpi": self.dpi,
            "darkness": self.darkness,
            "copies": self.copies,
            "orientation": self.orientation,
            "date_format": self.date_format,
            "time_format": self.time_format,
            "counter_next": self.counter_next,
            "counter_step": self.counter_step,
            "counter_digits": self.counter_digits,
            "elements": [e.to_dict() for e in self.elements],
        }
        for e in d["elements"]:
            e.pop("measured_w", None)
            e.pop("measured_h", None)
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Label":
        d = json.loads(text)
        if d.get("format") not in ("zplinux", "labelix"):   # ZPLinux was called Labelix
            raise ValueError("Not a ZPLinux label file")
        return cls(
            width_mm=float(d.get("width_mm", 101.6)),
            height_mm=float(d.get("height_mm", 152.4)),
            dpi=int(d.get("dpi", 203)),
            darkness=int(d.get("darkness", 0)),
            copies=int(d.get("copies", 1)),
            orientation=int(d.get("orientation", 0)) % 360,
            date_format=str(d.get("date_format", "%d/%m/%Y")),
            time_format=str(d.get("time_format", "%H:%M:%S")),
            counter_next=int(d.get("counter_next", 1)),
            counter_step=int(d.get("counter_step", 1)),
            counter_digits=int(d.get("counter_digits", 0)),
            elements=[element_from_dict(e) for e in d.get("elements", [])],
        )
