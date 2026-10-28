"""Tests for reading ZPL back into a label. Run: python3 -m unittest discover tests"""

import base64
import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zplinux import symbology, templates, zpl, zpl_import  # noqa: E402
from zplinux.model import (BarcodeElement, BoxElement, Code128Element,  # noqa: E402
                           EllipseElement, ImageElement, Label, QRElement, TextElement)

# Shaped like ZebraDesigner's "print to file" output: a printer set-up block, then
# ~DG graphics, then the label with ^FT positions and ^FH\ escapes.
DESIGNER = r"""CT~~CD,~CC^~CT~
^XA~TA000~JSN^LT0^MNW^MTD^PON^PMN^LH0,0^JMA^PR4,4~SD15^JUS^LRN^CI0^XZ
~DG000.GRF,00008,002,
FFFF,J0:
^XA
^MMT
^PW812
^LL0406
^LS0
^FT32,72^A0N,45,45^FH\^FDBlue Mug^FS
^FT32,110^A0N,28,28^FH\^FDSKU: MUG\5F0001^FS
^BY3,3,81^FT32,220^BCN,,Y,N
^FD>:MUG-0001^FS
^FT600,250^BQN,2,6
^FH\^FDLA,https://example.com^FS
^FO20,20^GB772,366,4^FS
^FO560,300^GB220,70,70^FS
^FT560,350^A0N,45,45^FB220,1,0,C^FR^FH\^FD$12.99^FS
^FT400,380^XG000.GRF,1,1^FS
^PQ2,0,1,Y^XZ
"""


def kinds(label):
    return [type(e) for e in label.elements]


class RoundTripTests(unittest.TestCase):
    def test_templates_round_trip(self):
        for name, _size, _sub, factory in templates.TEMPLATES:
            lab, values = factory()
            if not lab.elements:
                continue
            with self.subTest(template=name):
                out = zpl.label_zpl(lab, values)
                r = zpl_import.parse(out)
                self.assertEqual(zpl.label_zpl(r.label, r.values), out)
                self.assertEqual((r.label.width_dots, r.label.height_dots),
                                 (lab.width_dots, lab.height_dots))


class SymbologyRoundTripTests(unittest.TestCase):
    def test_every_type_round_trips(self):
        for sym in symbology.SYMBOLOGIES:
            with self.subTest(type=sym.key):
                el = BarcodeElement(x=30, y=40, symbology=sym.key, data=sym.example, height=60,
                                    module=3, ratio=2.5, columns=2 if sym.key == "pdf417" else 0,
                                    mode=sym.modes[0][0] if sym.modes else 0)
                out = zpl.label_zpl(Label(width_mm=101.6, height_mm=101.6, elements=[el]))
                r = zpl_import.parse(out)
                self.assertEqual(len(r.label.elements), 1)
                back = r.label.elements[0]
                self.assertEqual((back.symbology, back.data), (sym.key, sym.example))
                self.assertEqual(zpl.label_zpl(r.label), out)


class RotationRoundTripTests(unittest.TestCase):
    def test_rotated_objects_round_trip(self):
        for deg in (90, 180, 270):
            els = [TextElement(x=40, y=40, text="Hello", height=40, rotation=deg),
                   TextElement(x=300, y=40, text="one\ntwo", height=30, block_width=160,
                               max_lines=2, align="C", rotation=deg),
                   Code128Element(x=40, y=300, data="12345678", height=60, rotation=deg),
                   BarcodeElement(x=40, y=560, symbology="code39", data="AB12", height=60,
                                  rotation=deg),
                   BarcodeElement(x=330, y=560, symbology="datamatrix", data="DM", module=4,
                                  rotation=deg)]
            out = zpl.label_zpl(Label(width_mm=101.6, height_mm=101.6, elements=els))
            with self.subTest(rotation=deg):
                r = zpl_import.parse(out)
                self.assertEqual([e.rotation for e in r.label.elements], [deg] * 5)
                self.assertEqual(zpl.label_zpl(r.label), out)


class BarcodeTextTests(unittest.TestCase):
    def test_own_text_size_round_trips_as_one_element(self):
        lab = Label(width_mm=50.8, height_mm=25.4, elements=[
            Code128Element(x=40, y=20, data="12669", height=60, module=2, text_size=36)])
        out = zpl.label_zpl(lab)
        self.assertIn("^BCN,60,N,N,N,N", out)                # the printer's own text off
        self.assertIn("^A0N,36,36^FB", out)
        r = zpl_import.parse(out)
        self.assertEqual(len(r.label.elements), 1)
        bc = r.label.elements[0]
        self.assertEqual((bc.show_text, bc.text_size), (True, 36))
        self.assertEqual(zpl.label_zpl(r.label), out)

    def test_font_before_barcode_sets_text_size(self):
        r = zpl_import.parse("^XA^BY2^FO50,50^A0N,30,30^BCN,60,Y,N,N^FD12345678^FS^XZ")
        self.assertEqual(r.label.elements[0].text_size, 30)

    def test_unrelated_text_under_a_barcode_stays_text(self):
        r = zpl_import.parse("^XA^BY2^FO40,20^BCN,60,N,N,N^FDABC^FS"
                             "^FO40,82^A0N,30,30^FB200,1,0,C,0^FDsomething else^FS^XZ")
        self.assertEqual(len(r.label.elements), 2)


class DesignerFileTests(unittest.TestCase):
    def setUp(self):
        self.r = zpl_import.parse(DESIGNER)
        self.els = self.r.label.elements

    def test_size_copies_and_kinds(self):
        lab = self.r.label
        self.assertEqual((lab.dpi, lab.width_dots, lab.height_dots), (203, 812, 406))
        self.assertEqual(lab.copies, 2)
        self.assertEqual(kinds(lab), [TextElement, TextElement, Code128Element, QRElement,
                                      BoxElement, BoxElement, TextElement, ImageElement])

    def test_text_from_baseline(self):
        t = self.els[0]
        self.assertEqual((t.text, t.x, t.height), ("Blue Mug", 32, 45))
        self.assertEqual(t.y, 72 - 33)                         # 3/4 of 45, rounded down
        self.assertEqual(self.els[1].text, "SKU: MUG_0001")   # \5F with ^FH\

    def test_barcode(self):
        b = self.els[2]
        self.assertEqual((b.data, b.module, b.height, b.show_text), ("MUG-0001", 3, 81, True))
        self.assertEqual(b.y, 220 - 81)

    def test_qr(self):
        q = self.els[3]
        self.assertEqual((q.data, q.error_correction, q.magnification), ("https://example.com", "L", 6))
        self.assertLess(q.y, 250 - 21 * 6)

    def test_boxes(self):
        frame, solid = self.els[4], self.els[5]
        self.assertEqual((frame.w, frame.h, frame.thickness, frame.filled), (772, 366, 4, False))
        self.assertTrue(solid.filled)

    def test_reverse_centred_text(self):
        t = self.els[6]
        self.assertEqual((t.text, t.reverse, t.block_width, t.align), ("$12.99", True, 220, "C"))

    def test_downloaded_graphic(self):
        img = self.els[7]
        self.assertEqual((img.w, img.h), (16, 4))
        self.assertEqual(img.y, 380 - 4)
        self.assertTrue(img.image_bytes().startswith(b"\x89PNG"))

    def test_warns_about_assumptions_only(self):
        self.assertTrue(self.r.warnings[0].startswith("Assumed 203 dpi"))
        self.assertEqual(len(self.r.warnings), 1, self.r.warnings)


class DetailTests(unittest.TestCase):
    def test_ascii_compression(self):
        # 2 bytes per row: a full row, "J0" = ten zeros then the row ends with ',',
        # ':' repeats the previous row, '!' fills with ones.
        bits = zpl_import.decode_graphic("FFFF,:8!", 2)
        self.assertEqual(bits, bytes.fromhex("FFFF" "0000" "0000" "8FFF"))
        self.assertEqual(zpl_import.decode_graphic("JF", 2)[:2], bytes.fromhex("FFFF"))
        self.assertEqual(zpl_import.decode_graphic("gF", 10), b"\xff" * 10)

    def test_z64_graphic(self):
        raw = bytes(range(16))
        data = ":Z64:" + base64.b64encode(zlib.compress(raw)).decode() + ":1234"
        self.assertEqual(zpl_import.decode_graphic(data, 4), raw)

    def test_gfa_graphic(self):
        r = zpl_import.parse("^XA^FO10,20^GFA,4,4,2,F0F00F0F^FS^XZ")
        img = r.label.elements[0]
        self.assertIsInstance(img, ImageElement)
        self.assertEqual((img.x, img.y, img.w, img.h), (10, 20, 16, 2))

    def test_hex_escapes_and_block_newlines(self):
        r = zpl_import.parse(r"^XA^FO0,0^A0N,30,30^FB300,2,0,L,0^FH_^FDa_5Fb\&c\\d^FS^XZ")
        self.assertEqual(r.label.elements[0].text, "a_b\nc\\d")

    def test_other_fonts_rotation_and_barcodes_are_reported(self):
        r = zpl_import.parse("^XA^FO10,10^ADR,36,20^FDhi^FS"
                             "^FO10,100^BTN,2,3,40,4,8^FDABC^FS"
                             "^FO10,200^GE100,50,2^FS^FO300,10^GD50,50,2^FS^XZ")
        text = " ".join(r.warnings)
        for word in ("TLC39", "Diagonal"):
            self.assertIn(word, text)
        self.assertEqual(kinds(r.label), [TextElement, Code128Element, EllipseElement])
        self.assertEqual(r.label.elements[0].rotation, 90)          # ^ADR
        self.assertEqual(r.label.elements[0].font, "D")             # printer font kept

    def test_barcode_types_come_in_as_themselves(self):
        r = zpl_import.parse("^XA^BY3,2.5^FO10,100^B3N,Y,50,N,N^FDABC^FS"
                             "^FO10,10^BXN,6,200^FDdata matrix^FS^XZ")
        code39, dm = r.label.elements
        self.assertEqual((code39.symbology, code39.height, code39.module, code39.ratio,
                          code39.check, code39.show_text), ("code39", 50, 3, 2.5, True, False))
        self.assertEqual((dm.symbology, dm.module, dm.data), ("datamatrix", 6, "data matrix"))
        self.assertEqual(len(r.warnings), 1, r.warnings)

    def test_each_barcode_keeps_its_own_bar_width(self):
        r = zpl_import.parse("^XA^BY2^FO10,10^BCN,50,N^FDA^FS^BY4^FO10,100^BCN,50,N^FDB^FS^XZ")
        self.assertEqual([e.module for e in r.label.elements], [2, 4])

    def test_gs1_datamatrix(self):
        # FNC1 is ~1 with ~ as the escape character; a raw ~ would start a command, so
        # the data is written with ^FH (_7E is ~)
        r = zpl_import.parse("^XA^FO10,10^BXN,4,200,0,0,6,~,1"
                             "^FH_^FD_7E10109501101530003_7E110AB12^FS^XZ")
        el = r.label.elements[0]
        self.assertEqual((el.symbology, el.data), ("gs1-datamatrix", "(01)09501101530003(10)AB12"))

    def test_field_numbers_become_variables(self):
        r = zpl_import.parse("^XA^FO10,10^A0N,30,30^FN3^FDJane^FS^XZ")
        self.assertEqual(r.label.elements[0].text, "{field3}")
        self.assertEqual(r.values, {"field3": "Jane"})

    def test_label_home_offsets_positions(self):
        r = zpl_import.parse("^XA^LH20,30^FO5,5^GB10,10,1^FS^XZ")
        self.assertEqual((r.label.elements[0].x, r.label.elements[0].y), (25, 35))

    def test_wide_labels_mean_higher_resolution(self):
        r = zpl_import.parse("^XA^PW1200^FO5,5^GB10,10,1^FS^XZ")
        self.assertEqual(r.label.dpi, 300)

    def test_several_labels_import_the_first(self):
        r = zpl_import.parse("^XA^FO1,1^GB5,5,1^FS^XZ^XA^FO2,2^GB5,5,1^FS^XZ")
        self.assertEqual(len(r.label.elements), 1)
        self.assertTrue(any("only the first" in w for w in r.warnings))

    def test_nothing_to_import(self):
        with self.assertRaises(zpl_import.ZplImportError):
            zpl_import.parse("hello")
        with self.assertRaises(zpl_import.ZplImportError):
            zpl_import.parse("^XA^JUS^XZ")


if __name__ == "__main__":
    unittest.main()
