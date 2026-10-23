"""Label document model. All positions and sizes are in printer dots."""

from __future__ import annotations

import base64
import copy
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


@dataclass
class Element:
    kind = "element"
    x: int = 20
    y: int = 20

    def bounds(self) -> tuple:
        """(x, y, w, h) in dots, used for selection and hit-testing."""
        return (self.x, self.y, 10, 10)

    def resize(self, w: int, h: int) -> None:
        pass

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind
        return d

    def text_fields(self) -> List[str]:
        """Names of the fields that may hold {variables}."""
        return []


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
    # Filled in by the renderer so hit-testing matches what is drawn.
    measured_w: int = field(default=0, compare=False)
    measured_h: int = field(default=0, compare=False)

    def bounds(self):
        w = self.block_width or self.measured_w or max(10, len(self.text) * self.height // 2)
        h = self.measured_h or self.height * max(1, self.max_lines if self.block_width else 1)
        return (self.x, self.y, w, h)

    def resize(self, w, h):
        if self.block_width:
            self.block_width = max(10, w)
        else:
            self.height = max(10, h)

    def text_fields(self):
        return ["text"]


@dataclass
class Code128Element(Element):
    kind = "code128"
    data: str = "123456789"
    height: int = 80
    module: int = 2           # narrow bar width in dots (^BY)
    show_text: bool = True
    text_size: int = 0        # 0: the printer's own text; else a font 0 line of this height
    measured_w: int = field(default=0, compare=False)

    def bounds(self):
        w = self.measured_w or 200
        return (self.x, self.y, w, self.height + self._text_space())

    def text_gap(self) -> int:
        """Space between the bars and the text below them."""
        return max(2, self.module)

    def _text_space(self) -> int:
        if not self.show_text:
            return 0
        return (self.text_size or interpretation_height(self.module)) + self.text_gap()

    def resize(self, w, h):
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

    def bounds(self):
        s = self.measured_w or 25 * self.magnification
        return (self.x, self.y, s, s)

    def resize(self, w, h):
        modules = max(21, (self.measured_w or 25 * self.magnification) // self.magnification)
        self.magnification = max(1, min(10, round(max(w, h) / modules)))

    def text_fields(self):
        return ["data"]


@dataclass
class BoxElement(Element):
    kind = "box"
    w: int = 200
    h: int = 100
    thickness: int = 3
    rounding: int = 0         # 0-8
    filled: bool = False

    def bounds(self):
        return (self.x, self.y, self.w, self.h)

    def resize(self, w, h):
        self.w = max(1, w)
        self.h = max(1, h)


@dataclass
class EllipseElement(Element):
    kind = "ellipse"
    w: int = 150
    h: int = 100
    thickness: int = 3
    filled: bool = False

    def bounds(self):
        return (self.x, self.y, self.w, self.h)

    def resize(self, w, h):
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

    def bounds(self):
        return (self.x, self.y, self.w, self.h)

    def resize(self, w, h):
        if self.keep_aspect and self.w and self.h:
            ratio = self.h / self.w
            self.w = max(4, w)
            self.h = max(4, round(self.w * ratio))
        else:
            self.w = max(4, w)
            self.h = max(4, h)

    def image_bytes(self) -> bytes:
        return base64.b64decode(self.png_b64) if self.png_b64 else b""


def describe(el: Element) -> str:
    """Short human description for the layers list."""
    if isinstance(el, TextElement):
        return el.text.replace("\n", " ").strip() or "(empty text)"
    if isinstance(el, (Code128Element, QRElement)):
        return el.data or "(no data)"
    if isinstance(el, (BoxElement, EllipseElement, ImageElement)):
        return f"{el.w} × {el.h} dots"
    return el.kind


KIND_NAMES = {"text": "Text", "code128": "Barcode", "qr": "QR code", "box": "Box",
              "ellipse": "Ellipse", "image": "Image"}


KINDS: Dict[str, type] = {c.kind: c for c in (
    TextElement, Code128Element, QRElement, BoxElement, EllipseElement, ImageElement)}


def element_from_dict(d: dict) -> Element:
    cls = KINDS[d["kind"]]
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Label:
    width_mm: float = 101.6   # 4 x 6 inch shipping label by default
    height_mm: float = 152.4
    dpi: int = 203
    darkness: int = 0         # -30..30 relative (^MD); 0 = printer default
    copies: int = 1
    elements: List[Element] = field(default_factory=list)

    @property
    def width_dots(self) -> int:
        return round(self.width_mm * dpmm(self.dpi))

    @property
    def height_dots(self) -> int:
        return round(self.height_mm * dpmm(self.dpi))

    def variables(self) -> List[str]:
        """Variable names used anywhere in the label, in order of first use."""
        seen: List[str] = []
        for el in self.elements:
            for name in el.text_fields():
                for var in VAR_RE.findall(getattr(el, name)):
                    if var not in seen:
                        seen.append(var)
        return seen

    def filled(self, values: Optional[Dict[str, str]]) -> "Label":
        """A copy with {variables} replaced. Unknown variables are left as they are."""
        out = copy.deepcopy(self)
        if not values:
            return out
        for el in out.elements:
            for name in el.text_fields():
                setattr(el, name, VAR_RE.sub(
                    lambda m: str(values.get(m.group(1), m.group(0))), getattr(el, name)))
        return out

    def to_json(self) -> str:
        d = {
            "format": "labelix",
            "version": 1,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "dpi": self.dpi,
            "darkness": self.darkness,
            "copies": self.copies,
            "elements": [e.to_dict() for e in self.elements],
        }
        for e in d["elements"]:
            e.pop("measured_w", None)
            e.pop("measured_h", None)
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Label":
        d = json.loads(text)
        if d.get("format") != "labelix":
            raise ValueError("Not a Labelix label file")
        return cls(
            width_mm=float(d.get("width_mm", 101.6)),
            height_mm=float(d.get("height_mm", 152.4)),
            dpi=int(d.get("dpi", 203)),
            darkness=int(d.get("darkness", 0)),
            copies=int(d.get("copies", 1)),
            elements=[element_from_dict(e) for e in d.get("elements", [])],
        )
