"""Draw barcode previews with the zint command-line tool.

zint writes the symbol as SVG with one unit per module; this module runs it,
reads the bars, modules and MaxiCode hexagons back as shapes in module units,
and caches the result. The printer draws the real barcode from ZPL, so without
zint only the preview is missing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional, Tuple

# For running a zint that is not on PATH (tests, development).
ZINT_ENV = "ZPLINUX_ZINT"


class ZintError(Exception):
    pass


@dataclass(frozen=True)
class Symbol:
    width: float                     # in modules
    height: float
    rects: Tuple[Tuple[float, float, float, float], ...] = ()   # x, y, w, h
    hexagons: Tuple[Tuple[Tuple[float, float], ...], ...] = ()  # MaxiCode modules
    circles: Tuple[Tuple[float, float, float, float], ...] = ()  # cx, cy, r, stroke


def zint_path() -> Optional[str]:
    return os.environ.get(ZINT_ENV) or shutil.which("zint")


def available() -> bool:
    return zint_path() is not None


_NUM = r"(-?[\d.]+)"
_RECT = re.compile(rf"M{_NUM} {_NUM}h{_NUM}v{_NUM}h-?[\d.]+Z")
_POLY = re.compile(r"M([^ML]+?(?:L[^ML]+?)+)Z")
_CIRCLE = re.compile(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"'
                     r'(?: stroke="[^"]*" stroke-width="([\d.]+)")?')


def parse_svg(svg: str) -> Symbol:
    m = re.search(r'<svg width="([\d.]+)" height="([\d.]+)"', svg)
    if not m:
        raise ZintError("zint gave no symbol")
    width, height = float(m.group(1)), float(m.group(2))
    rects, hexes = [], []
    for d in re.findall(r'<path d="([^"]+)"', svg):
        for r in _RECT.finditer(d):
            rects.append(tuple(float(v) for v in r.groups()))
        if "L" in d:
            for poly in _POLY.findall(d):
                pts = tuple(tuple(float(v) for v in p.split()) for p in poly.split("L"))
                hexes.append(pts)
    circles = [(float(a), float(b), float(c), float(s or 0))
               for a, b, c, s in _CIRCLE.findall(svg)]
    return Symbol(width, height, tuple(rects), tuple(hexes), tuple(circles))


@lru_cache(maxsize=512)
def encode(zint_type: int, data: str, options: Tuple[str, ...] = ()) -> Symbol:
    """The symbol for data, in module units. Raises ZintError with zint's message."""
    exe = zint_path()
    if exe is None:
        raise ZintError("Install zint to see this barcode in the preview.")
    cmd = [exe, "--direct", "--filetype=svg", "--notext", "--scale=0.5", "--noquietzones",
           f"--barcode={zint_type}", *options, f"--data={data}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ZintError(f"zint did not run: {e}")
    if r.returncode != 0 or "<svg" not in r.stdout:
        msg = (r.stderr or r.stdout).strip().splitlines()
        # "Error 773: Input too long ..." -> the message part
        text = msg[-1] if msg else "zint could not encode this data"
        raise ZintError(re.sub(r"^(Error|Warning) \d+: ", "", text))
    return parse_svg(r.stdout)
