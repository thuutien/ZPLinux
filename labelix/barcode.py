"""Barcode encoders used for the on-screen preview.

The printer draws the real barcode from ZPL; these only need to produce the
same module layout so the preview matches what will be printed.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

# Code 128 symbol widths (bar, space, bar, space, bar, space) for values 0-106.
_C128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112"
).split()
START_B = 104
START_C = 105
CODE_B = 100       # switch to subset B (from C)
CODE_C = 99        # switch to subset C (from B)
STOP = 106


def _digit_run(data: str, i: int) -> int:
    n = i
    while n < len(data) and data[n].isdigit() and data[n].isascii():
        n += 1
    return n - i


def code128_segments(data: str) -> Optional[List[Tuple[str, str]]]:
    """Split data into subset B and C runs, [("C", "1266"), ("B", "9")], following
    ISO/IEC 15417 Annex E as ZebraDesigner does: start in C for 4 or more leading digits,
    switch to C for 4+ digits at the end or 6+ in the middle, and keep an odd digit in B.
    None if a character is outside subset B."""
    if any(not 32 <= ord(c) <= 127 for c in data):
        return None
    segs: List[Tuple[str, str]] = []

    def add(subset, text):
        if segs and segs[-1][0] == subset:
            segs[-1] = (subset, segs[-1][1] + text)
        else:
            segs.append((subset, text))

    i = 0
    lead = _digit_run(data, 0)
    if lead >= 4 or (lead == 2 and len(data) == 2):
        even = lead - lead % 2
        add("C", data[:even])
        i = even
    while i < len(data):
        run = _digit_run(data, i)
        at_end = i + run == len(data)
        if run >= 6 or (run >= 4 and at_end):
            if run % 2:
                add("B", data[i])
                i += 1
                run -= 1
            add("C", data[i:i + run])
            i += run
        else:
            add("B", data[i])
            i += 1
    return segs


def code128_values(data: str) -> Optional[List[int]]:
    """Symbol values including start, subset switches, check digit and stop."""
    segs = code128_segments(data)
    if segs is None:
        return None
    if not segs:
        segs = [("B", "")]
    values = [START_C if segs[0][0] == "C" else START_B]
    for n, (subset, text) in enumerate(segs):
        if n:
            values.append(CODE_C if subset == "C" else CODE_B)
        if subset == "C":
            values.extend(int(text[k:k + 2]) for k in range(0, len(text), 2))
        else:
            values.extend(ord(c) - 32 for c in text)
    check = values[0] + sum(i * v for i, v in enumerate(values[1:], start=1))
    values.append(check % 103)
    values.append(STOP)
    return values


def code128(data: str) -> Optional[List[int]]:
    """Alternating bar/space widths in modules for `data`, or None if a character
    cannot be encoded."""
    values = code128_values(data)
    if values is None:
        return None
    return [int(c) for v in values for c in _C128[v]]


def code128_zpl_data(data: str) -> Optional[str]:
    """^BC mode N field data with explicit subset codes (>; start C, >: start B,
    >5 to C, >6 to B), so the printer draws exactly the bars the preview shows."""
    segs = code128_segments(data)
    if segs is None:
        return None
    out = []
    for n, (subset, text) in enumerate(segs or [("B", "")]):
        out.append((">;" if subset == "C" else ">:") if n == 0 else
                   (">5" if subset == "C" else ">6"))
        out.append(text if subset == "C" else text.replace(">", ">0"))
    return "".join(out)


def code128_modules(data: str) -> int:
    """Width of the subset B encoding, for the placeholder drawn when the data
    cannot be encoded."""
    return 11 * (len(data) + 2) + 13


def qr_matrix(data: str, error_correction: str = "Q") -> Optional[List[List[bool]]]:
    """QR module matrix without quiet zone, or None if the qrcode module is missing."""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q
    except ImportError:
        return None
    level = {"H": ERROR_CORRECT_H, "Q": ERROR_CORRECT_Q,
             "M": ERROR_CORRECT_M, "L": ERROR_CORRECT_L}[error_correction]
    qr = qrcode.QRCode(error_correction=level, border=0)
    qr.add_data(data or " ")
    qr.make(fit=True)
    return qr.get_matrix()
