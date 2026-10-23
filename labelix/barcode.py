"""Barcode encoders used for the on-screen preview.

The printer draws the real barcode from ZPL; these only need to produce the
same module layout so the preview matches what will be printed.
"""

from __future__ import annotations

from typing import List, Optional

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
STOP = 106


def code128_b(data: str) -> Optional[List[int]]:
    """Alternating bar/space widths in modules for `data` in subset B, or None
    if a character cannot be encoded in subset B."""
    values = [START_B]
    for ch in data:
        o = ord(ch)
        if not 32 <= o <= 127:
            return None
        values.append(o - 32)
    check = values[0]
    for i, v in enumerate(values[1:], start=1):
        check += i * v
    values.append(check % 103)
    values.append(STOP)
    return [int(c) for v in values for c in _C128[v]]


def code128_modules(data: str) -> int:
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
