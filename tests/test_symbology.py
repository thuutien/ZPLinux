"""Tests for the extra barcode types. Run: python3 -m unittest discover tests

The preview tests need the zint command (or ZPLINUX_ZINT pointing at one) and are
skipped without it; the ZPL the printer gets does not depend on zint.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zplinux import symbology, zint, zpl  # noqa: E402
from zplinux.model import BarcodeElement, Label, element_from_dict  # noqa: E402

needs_zint = unittest.skipUnless(zint.available(), "zint is not installed")

SVG_BARS = """<svg width="7" height="50" version="1.1" xmlns="http://www.w3.org/2000/svg">
 <g id="barcode" fill="#000000">
  <rect x="0" y="0" width="7" height="50" fill="#FFFFFF"/>
  <path d="M0 0h1v50h-1ZM3 0h2v50h-2ZM6 6h1v6h-1Z"/>
 </g>
</svg>"""

SVG_MAXI = """<svg width="30" height="29" version="1.1" xmlns="http://www.w3.org/2000/svg">
  <path d="M1.5 1.08L1.93 0.83L1.93 0.33L1.5 0.08L1.07 0.33L1.07 0.83Z"/>
  <circle cx="14.5" cy="14.43" r="4.108" stroke="#000000" stroke-width="0.785" fill="none"/>
</svg>"""


class SymbologyZplTests(unittest.TestCase):
    def zpl(self, **kw):
        return zpl.element_zpl(BarcodeElement(x=10, y=20, **kw))[0]

    def test_two_width_codes_write_the_ratio(self):
        self.assertEqual(self.zpl(symbology="code39", data="AB", module=3, ratio=2.5, check=True),
                         "^BY3,2.5^FO10,20^B3N,Y,80,Y,N^FH_^FDAB^FS")

    def test_other_codes_write_only_the_module(self):
        self.assertEqual(self.zpl(symbology="ean13", data="590123412345", height=60,
                                  show_text=False),
                         "^BY2^FO10,20^BEN,60,N,N^FH_^FD590123412345^FS")

    def test_gs1_datamatrix_writes_fnc1(self):
        out = self.zpl(symbology="gs1-datamatrix", data="(01)09501101530003(10)AB", module=4)
        self.assertIn("^BXN,4,200,0,0,6,~,1", out)
        self.assertIn("^FD_7E10109501101530003_7E110AB^FS", out)    # ~1 before each AI

    def test_every_type_has_its_command_and_example(self):
        for sym in symbology.SYMBOLOGIES:
            with self.subTest(type=sym.key):
                out = self.zpl(symbology=sym.key, data=sym.example)
                self.assertIn("^" + sym.command, out)

    def test_check_digit_in_the_text(self):
        self.assertEqual(symbology.display_text(
            BarcodeElement(symbology="ean13", data="590123412345")), "5901234123457")
        self.assertEqual(symbology.display_text(
            BarcodeElement(symbology="upca", data="01234567890")), "012345678905")

    def test_saved_files_keep_the_type(self):
        el = BarcodeElement(symbology="pdf417", data="x", columns=3, security=2)
        back = element_from_dict(el.to_dict())
        self.assertEqual((back.symbology, back.columns, back.security), ("pdf417", 3, 2))


class ZintSvgTests(unittest.TestCase):
    def test_bars(self):
        sym = zint.parse_svg(SVG_BARS)
        self.assertEqual((sym.width, sym.height), (7, 50))
        self.assertEqual(sym.rects, ((0, 0, 1, 50), (3, 0, 2, 50), (6, 6, 1, 6)))

    def test_maxicode_hexagons_and_rings(self):
        sym = zint.parse_svg(SVG_MAXI)
        self.assertEqual(len(sym.hexagons), 1)
        self.assertEqual(len(sym.hexagons[0]), 6)
        self.assertEqual(sym.circles, ((14.5, 14.43, 4.108, 0.785),))

    def test_missing_zint(self):
        old = os.environ.get(zint.ZINT_ENV)
        os.environ[zint.ZINT_ENV] = "/nonexistent/zint"
        try:
            with self.assertRaises(zint.ZintError):
                zint.encode.__wrapped__(8, "AB", ())
        finally:
            if old is None:
                del os.environ[zint.ZINT_ENV]
            else:
                os.environ[zint.ZINT_ENV] = old


@needs_zint
class PreviewTests(unittest.TestCase):
    def draw(self, el):
        import cairo
        from zplinux import render
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 800, 600)
        render.draw_label(cairo.Context(surf), Label(width_mm=100, height_mm=75, elements=[el]))
        return el

    def test_every_example_encodes(self):
        for sym in symbology.SYMBOLOGIES:
            el = BarcodeElement(symbology=sym.key, data=sym.example,
                                mode=sym.modes[0][0] if sym.modes else 0)
            with self.subTest(type=sym.key):
                zint.encode(sym.zint_type_for(el), sym.zint_data(el.data, el),
                            tuple(sym.zint_options(el)))

    def test_wide_bars_follow_the_ratio(self):
        # Code 39 "A": 13 narrow and 3 wide elements per character; the printer makes
        # wide bars int(ratio * module) dots, so 2.5 x 3 = 7
        a = self.draw(BarcodeElement(symbology="code39", data="A", module=3, ratio=3.0))
        b = self.draw(BarcodeElement(symbology="code39", data="A", module=3, ratio=2.5))
        self.assertGreater(a.measured_w, b.measured_w)
        self.assertEqual((a.measured_w - b.measured_w) % 2, 0)

    def test_bad_data_draws_a_placeholder(self):
        el = self.draw(BarcodeElement(symbology="ean13", data="not digits"))
        self.assertEqual(el.measured_w, 220)

    def test_pdf417_columns_are_written_explicitly(self):
        out = zpl.element_zpl(BarcodeElement(symbology="pdf417", data="PDF417 example",
                                             height=6))[0]
        self.assertIn("^B7N,6,0,2,0,N", out)


if __name__ == "__main__":
    unittest.main()
