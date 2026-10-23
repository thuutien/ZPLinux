"""The barcode types besides Code 128 and QR: how each is written in ZPL and drawn
with zint for the preview.

Every type is a `Symbology`. `zpl()` returns the ^Bx command (after ^BY and ^FO),
`zint()` the zint barcode number, options and data for the preview, and `options`
names the BarcodeElement settings it uses, which the properties panel shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# Settings a BarcodeElement can have; each symbology uses some of them.
HEIGHT, RATIO, TEXT, CHECK, COLUMNS, ROWS, SECURITY, MODE = (
    "height", "ratio", "show_text", "check", "columns", "rows", "security", "mode")


@dataclass
class Symbology:
    key: str
    name: str
    group: str                      # "1D", "2D" or "Postal and legacy"
    command: str                    # the ZPL command, e.g. "B3"
    zint_type: int
    example: str
    hint: str
    zpl: Callable[["object"], str]
    options: Tuple[str, ...] = (HEIGHT, TEXT)
    shape: str = "linear"           # linear, stacked, matrix, postal, maxicode
    two_width: bool = False         # bars are narrow or wide (^BY ratio applies)
    zint_options: Callable[["object"], List[str]] = field(default=lambda el: [])
    zint_data: Callable[[str, "object"], str] = field(default=lambda data, el: data)
    zpl_data: Callable[[str, "object"], str] = field(default=lambda data, el: data)
    modes: Tuple[Tuple[int, str], ...] = ()   # choices for the MODE setting
    zint_types: Dict[int, int] = field(default_factory=dict)  # MODE -> zint type, if it varies
    always_text: bool = False       # the printer always prints the text (LOGMARS)

    def zint_type_for(self, el) -> int:
        return self.zint_types.get(el.mode, self.zint_type)


def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


def _rows_mult(el) -> int:
    """Commands whose height is a multiple of the module: row height in dots / module."""
    return max(1, round(el.height / max(1, el.module)))


def gs1_brackets(data: str) -> str:
    """'(01)123(10)AB' -> '[01]123[10]AB' for zint."""
    return re.sub(r"\((\d{2,4})\)", r"[\1]", data)


# GS1 AIs with fixed-length data, by AI prefix: data length (the others run to the end
# or to an FNC1).
_GS1_FIXED = {"00": 18, "01": 14, "02": 14, "03": 14, "04": 16, "11": 6, "12": 6, "13": 6,
              "14": 6, "15": 6, "16": 6, "17": 6, "18": 6, "19": 6, "20": 2, "31": 6,
              "32": 6, "33": 6, "34": 6, "35": 6, "36": 6, "41": 13}
_AI_LEN = {**{p: 2 for p in ("00", "01", "02", "03", "04", "11", "12", "13", "14", "15", "16",
                             "17", "18", "19", "20")},
           **{p: 4 for p in ("31", "32", "33", "34", "35", "36")}, "41": 3}


def gs1_parenthesize(data: str) -> str:
    """'010950110153000310ABC' -> '(01)09501101530003(10)ABC': AIs written inline, as
    ^BR expects, back into parentheses. A variable-length AI takes the rest."""
    if data.startswith("(") or not data[:2].isdigit():
        return data
    out, i = [], 0
    while i < len(data):
        prefix = data[i:i + 2]
        ai_len = _AI_LEN.get(prefix, 2)
        ai = data[i:i + ai_len]
        i += ai_len
        n = _GS1_FIXED.get(prefix)
        value = data[i:i + n] if n else data[i:]
        i += len(value)
        out.append(f"({ai}){value}")
    return "".join(out)


def gs1_fnc1(data: str) -> str:
    """'(01)123(10)AB' -> '~1' before each AI, for ^BX with ~ as its escape character."""
    return re.sub(r"\((\d{2,4})\)", r"~1\1", data)


# Data Matrix ECC 200 sizes in zint's --vers order: (rows, columns).
_DM_SIZES = [(10, 10), (12, 12), (14, 14), (16, 16), (18, 18), (20, 20), (22, 22), (24, 24),
             (26, 26), (32, 32), (36, 36), (40, 40), (44, 44), (48, 48), (52, 52), (64, 64),
             (72, 72), (80, 80), (88, 88), (96, 96), (104, 104), (120, 120), (132, 132),
             (144, 144), (8, 18), (8, 32), (12, 26), (12, 36), (16, 36), (16, 48)]


def datamatrix_size(el) -> List[str]:
    """zint options for the symbol ^BX prints: square and automatic unless both rows and
    columns are given, encoded the simple way printers do (zint's --fast)."""
    if el.rows and el.columns and (el.rows, el.columns) in _DM_SIZES:
        return ["--fast", f"--vers={_DM_SIZES.index((el.rows, el.columns)) + 1}"]
    return ["--fast", "--square"]


def pdf417_columns(el) -> int:
    """Columns for ^B7. Left at 0, printers pick their own layout, so write the one the
    preview shows (zint's) when zint is installed."""
    if el.columns:
        return el.columns
    from . import zint
    try:
        sym = zint.encode(55, el.data, (f"--secure={el.security}",)
                          + ((f"--rows={el.rows}",) if el.rows else ()))
    except zint.ZintError:
        return 0
    # start, left and right row indicators and stop take 69 modules, each column 17
    return max(1, round((sym.width - 69) / 17))


# MicroPDF417 sizes in ^BF mode order (0-33): (columns, rows).
MICROPDF_MODES = ([(1, r) for r in (11, 14, 17, 20, 24, 28)]
                  + [(2, r) for r in (8, 11, 14, 17, 20, 23, 26)]
                  + [(3, r) for r in (6, 8, 10, 12, 15, 20, 26, 32, 38, 44)]
                  + [(4, r) for r in (4, 6, 8, 10, 12, 15, 20, 26, 32, 38, 44)])
_MICROPDF_WIDTHS = {38: 1, 55: 2, 82: 3, 99: 4}


def micropdf_mode(el) -> int:
    """^BF picks the symbol size by mode; write the size the preview shows (zint's)."""
    from . import zint
    try:
        sym = zint.encode(84, el.data, ())
    except zint.ZintError:
        return 0
    size = (_MICROPDF_WIDTHS.get(int(sym.width), 1), int(sym.height // 2))
    return MICROPDF_MODES.index(size) if size in MICROPDF_MODES else 0


def codablock_columns(el):
    """Characters per row for ^BB: the user's, or zint's layout when left automatic."""
    if el.columns:
        return el.columns
    from . import zint
    try:
        sym = zint.encode(74, el.data, (f"--rows={el.rows}",) if el.rows else ())
    except zint.ZintError:
        return ""
    # zint's columns are 11 modules each plus 2, and include 5 that aren't data
    return max(2, round((sym.width - 2) / 11) - 5)


SYMBOLOGIES: List[Symbology] = [
    # ---- 1D ----
    Symbology("gs1-128", "GS1-128", "1D", "BC", 16, "(01)09501101530003",
              "GS1 data with application identifiers in parentheses.",
              lambda el: f"^BCN,{el.height},{_yn(el.show_text)},N,N,D",
              zint_data=lambda d, el: gs1_brackets(d)),
    Symbology("code39", "Code 39", "1D", "B3", 8, "ABC-123",
              "Capital letters, digits, space and - . $ / + %",
              lambda el: f"^B3N,{_yn(el.check)},{el.height},{_yn(el.show_text)},N",
              options=(HEIGHT, TEXT, RATIO, CHECK), two_width=True,
              zint_options=lambda el: ["--vers=1"] if el.check else []),
    Symbology("code93", "Code 93", "1D", "BA", 25, "ABC-123",
              "Capital letters, digits, space and - . $ / + %",
              lambda el: f"^BAN,{el.height},{_yn(el.show_text)},N,N"),
    Symbology("ean13", "EAN-13", "1D", "BE", 13, "590123412345",
              "12 digits; the printer adds the check digit.",
              lambda el: f"^BEN,{el.height},{_yn(el.show_text)},N"),
    Symbology("ean8", "EAN-8", "1D", "B8", 13, "9638507",
              "7 digits; the printer adds the check digit.",
              lambda el: f"^B8N,{el.height},{_yn(el.show_text)},N"),
    Symbology("upca", "UPC-A", "1D", "BU", 34, "01234567890",
              "11 digits; the printer adds the check digit.",
              lambda el: f"^BUN,{el.height},{_yn(el.show_text)},N,Y"),
    Symbology("upce", "UPC-E", "1D", "B9", 37, "0123456",
              "7 digits (number system 0 or 1 and 6 digits); the printer adds the check digit.",
              lambda el: f"^B9N,{el.height},{_yn(el.show_text)},N,Y"),
    Symbology("upcean-ext", "UPC/EAN extension", "1D", "BS", 13, "12",
              "2 or 5 digits, printed beside an EAN or UPC barcode.",
              lambda el: f"^BSN,{el.height},{_yn(el.show_text)},N"),
    Symbology("databar", "GS1 DataBar", "1D", "BR", 29, "0950110153000",
              "13 digits (GTIN without its check digit); Expanded takes GS1 data in "
              "parentheses.",
              lambda el: f"^BRN,{el.mode or 1},{el.module},1,{el.height},22",
              options=(MODE,), shape="scaled",
              modes=((1, "Omnidirectional"), (2, "Truncated"), (3, "Stacked"),
                     (4, "Stacked omnidirectional"), (5, "Limited"), (6, "Expanded")),
              zint_types={3: 79, 4: 80, 5: 30, 6: 31},
              # DataBar heights are fixed by the standard (Truncated is 13 modules).
              zint_options=lambda el: ["--compliantheight"]
              + (["--height=13"] if el.mode == 2 else []),
              zint_data=lambda d, el: gs1_brackets(d) if el.mode == 6 else d,
              # ^BR reads the AIs itself: write them inline, without parentheses
              zpl_data=lambda d, el: re.sub(r"[()]", "", d) if el.mode == 6 else d),
    Symbology("i2of5", "Interleaved 2 of 5", "1D", "B2", 3, "12345678",
              "An even number of digits (with a check digit, an odd number).",
              lambda el: f"^B2N,{el.height},{_yn(el.show_text)},N,{_yn(el.check)}",
              options=(HEIGHT, TEXT, RATIO, CHECK), two_width=True),
    # ---- 2D ----
    Symbology("datamatrix", "Data Matrix", "2D", "BX", 71, "Data Matrix",
              "Any text. Rows and columns 0 pick the smallest size that fits.",
              lambda el: f"^BXN,{el.module},200,{el.columns},{el.rows},6,~,1",
              options=(COLUMNS, ROWS), shape="matrix",
              zint_options=lambda el: datamatrix_size(el)),
    Symbology("gs1-datamatrix", "GS1 DataMatrix", "2D", "BX", 71, "(01)09501101530003",
              "GS1 data with application identifiers in parentheses.",
              lambda el: f"^BXN,{el.module},200,{el.columns},{el.rows},6,~,1",
              options=(COLUMNS, ROWS), shape="matrix",
              zint_options=lambda el: ["--gs1"] + datamatrix_size(el),
              zint_data=lambda d, el: gs1_brackets(d),
              zpl_data=lambda d, el: gs1_fnc1(d)),
    Symbology("pdf417", "PDF417", "2D", "B7", 55, "PDF417 example",
              "Any text. Height is the height of one row.",
              lambda el: (f"^B7N,{el.height},{el.security},{pdf417_columns(el)},"
                          f"{el.rows or 0},N"),
              options=(HEIGHT, COLUMNS, ROWS, SECURITY), shape="stacked",
              zint_options=lambda el: [f"--secure={el.security}"]
              + ([f"--cols={el.columns}"] if el.columns else [])
              + ([f"--rows={el.rows}"] if el.rows else [])),
    Symbology("micropdf417", "MicroPDF417", "2D", "BF", 84, "MicroPDF",
              "Short text. Height is the height of one row.",
              lambda el: f"^BFN,{_rows_mult(el)},{micropdf_mode(el)}",
              options=(HEIGHT,), shape="stacked"),
    Symbology("aztec", "Aztec", "2D", "BO", 92, "Aztec",
              "Any text.",
              lambda el: f"^BON,{el.module},N,0,N,1,0",
              options=(), shape="matrix"),
    Symbology("maxicode", "MaxiCode", "2D", "BD", 57, "MaxiCode",
              "Up to 93 characters; a fixed size of about 1 × 1 inch.",
              lambda el: f"^BD{el.mode or 4},1,1",
              options=(MODE,), shape="maxicode",
              zint_options=lambda el: [f"--mode={el.mode or 4}"],
              modes=((4, "Standard"), (5, "Full error correction"), (6, "Reader programming"))),
    Symbology("codablock", "Codablock F", "2D", "BB", 74, "Codablock F",
              "Any text, in stacked rows. Height is the height of one row.",
              lambda el: f"^BBN,{el.height},Y,{codablock_columns(el)},{el.rows or ''},F",
              options=(HEIGHT, COLUMNS, ROWS), shape="stacked",
              zint_options=lambda el: ([f"--cols={el.columns + 5}"] if el.columns else [])
              + ([f"--rows={el.rows}"] if el.rows else [])),
    # ---- postal and legacy ----
    Symbology("codabar", "Codabar", "Postal and legacy", "BK", 18, "123456",
              "Digits and - $ : / . +; printed between A start and stop characters.",
              lambda el: f"^BKN,N,{el.height},{_yn(el.show_text)},N,A,A",
              options=(HEIGHT, TEXT, RATIO), two_width=True,
              zint_data=lambda d, el: f"A{d}A"),
    Symbology("code11", "Code 11", "Postal and legacy", "B1", 1, "123-45",
              "Digits and dashes; two check digits are added.",
              lambda el: f"^B1N,N,{el.height},{_yn(el.show_text)},N",
              options=(HEIGHT, TEXT, RATIO), two_width=True),
    Symbology("code49", "Code 49", "Postal and legacy", "B4", 24, "CODE 49",
              "Short text in stacked rows. Height is the height of one row.",
              lambda el: f"^B4N,{_rows_mult(el)},{'B' if el.show_text else 'N'},A",
              shape="stacked"),
    Symbology("industrial2of5", "Industrial 2 of 5", "Postal and legacy", "BI", 7, "123456",
              "Digits.",
              lambda el: f"^BIN,{el.height},{_yn(el.show_text)},N",
              options=(HEIGHT, TEXT, RATIO), two_width=True),
    Symbology("standard2of5", "Standard 2 of 5", "Postal and legacy", "BJ", 4, "123456",
              "Digits.",
              lambda el: f"^BJN,{el.height},{_yn(el.show_text)},N",
              options=(HEIGHT, TEXT, RATIO), two_width=True),
    Symbology("logmars", "LOGMARS", "Postal and legacy", "BL", 50, "ABC123",
              "Capital letters and digits; a Code 39 variant with a check digit.",
              lambda el: f"^BLN,{el.height},N",
              options=(HEIGHT, RATIO), two_width=True, always_text=True,
              zint_options=lambda el: ["--vers=1"]),
    Symbology("msi", "MSI", "Postal and legacy", "BM", 47, "123456",
              "Digits; one modulo 10 check digit is added.",
              lambda el: f"^BMN,B,{el.height},{_yn(el.show_text)},N,N",
              options=(HEIGHT, TEXT, RATIO), two_width=True,
              zint_options=lambda el: ["--vers=1"]),
    Symbology("plessey", "Plessey", "Postal and legacy", "BP", 86, "1234ABCD",
              "Digits and A-F.",
              lambda el: f"^BPN,N,{el.height},{_yn(el.show_text)},N",
              options=(HEIGHT, TEXT, RATIO), two_width=True),
    Symbology("planet", "Planet", "Postal and legacy", "B5", 82, "12345678901",
              "11 or 13 digits.",
              lambda el: f"^B5N,{el.height},{_yn(el.show_text)},N",
              shape="postal"),
    Symbology("postnet", "POSTNET", "Postal and legacy", "BZ", 40, "12345",
              "5, 9 or 11 digits (ZIP, ZIP+4 or delivery point).",
              lambda el: f"^BZN,{el.height},{_yn(el.show_text)},N,0",
              shape="postal"),
]

BY_KEY: Dict[str, Symbology] = {s.key: s for s in SYMBOLOGIES}


def gs1_check_digit(digits: str) -> str:
    """The modulo 10 check digit of EAN, UPC and GTIN numbers."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def display_text(el) -> str:
    """The text printed under a barcode: EAN-13, EAN-8 and UPC-A show the check digit
    the printer adds."""
    if el.symbology == "code39":
        return f"*{el.data}*"          # the printer shows the start and stop characters
    lengths = {"ean13": 12, "ean8": 7, "upca": 11}
    n = lengths.get(el.symbology)
    if n and len(el.data) == n and el.data.isdigit():
        return el.data + gs1_check_digit(el.data)
    return el.data
GROUPS = ("1D", "2D", "Postal and legacy")


def get(key: str) -> Optional[Symbology]:
    return BY_KEY.get(key)
