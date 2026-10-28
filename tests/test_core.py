"""Tests for the label model, ZPL output and printing. Run: python3 -m unittest discover tests"""

import base64
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zplinux import barcode, printing, zpl  # noqa: E402
from zplinux.model import (BoxElement, Code128Element, ImageElement, Label, QRElement,  # noqa: E402
                           TextElement, rotate_rect)

# 2x2 PNG: black, white / white, black
PNG_2X2 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAAAAABX3VL4AAAADklEQVR4nGNg+M/wnwEABgAB/4/x/JoAAAAASUVORK5CYII=")


class ModelTests(unittest.TestCase):
    def test_dots(self):
        lab = Label(width_mm=101.6, height_mm=50.8, dpi=203)
        self.assertEqual((lab.width_dots, lab.height_dots), (813, 406))
        lab.dpi = 300
        self.assertEqual(lab.width_dots, 1219)

    def test_variables_and_fill(self):
        lab = Label(elements=[TextElement(text="Hi {name} {name}"), Code128Element(data="{sku}"),
                              BoxElement()])
        self.assertEqual(lab.variables(), ["name", "sku"])
        filled = lab.filled({"name": "Ann"})
        self.assertEqual(filled.elements[0].text, "Hi Ann Ann")
        self.assertEqual(filled.elements[1].data, "{sku}")
        self.assertEqual(lab.elements[0].text, "Hi {name} {name}")  # original untouched

    def test_json_roundtrip(self):
        lab = Label(width_mm=50.0, height_mm=30.0, dpi=300, copies=3,
                    elements=[TextElement(text="x", reverse=True), QRElement(data="q"),
                              ImageElement(png_b64=base64.b64encode(PNG_2X2).decode(), w=2, h=2)])
        again = Label.from_json(lab.to_json())
        self.assertEqual(again.to_json(), lab.to_json())
        self.assertIsInstance(again.elements[1], QRElement)

    def test_rejects_other_json(self):
        with self.assertRaises(ValueError):
            Label.from_json('{"hello": 1}')


class ZplTests(unittest.TestCase):
    def test_structure(self):
        lab = Label(width_mm=50.8, height_mm=25.4, dpi=203, copies=2,
                    elements=[TextElement(x=5, y=6, text="A", height=30)])
        out = zpl.label_zpl(lab)
        self.assertTrue(out.startswith("^XA\n^CI28\n^PW406\n^LL203\n"))
        self.assertIn("^FO5,6^A0N,30,30^FH_^FDA^FS", out)
        self.assertIn("^PQ2", out)
        self.assertTrue(out.rstrip().endswith("^XZ"))

    def test_newlines_need_a_text_block(self):
        out = zpl.label_zpl(Label(elements=[TextElement(text="a\nb")]))
        self.assertIn("^FDa b^FS", out)
        out = zpl.label_zpl(Label(elements=[TextElement(text="a\nb", block_width=100, max_lines=2)]))
        self.assertIn("^FDa\\&b^FS", out)

    def test_escaping(self):
        self.assertEqual(zpl.escape("a^b~c_d\\e"), "a_5Eb_7Ec_5Fd\\e")

    def test_code128_gt_escaped(self):
        out = zpl.label_zpl(Label(elements=[Code128Element(data="A>B")]))
        self.assertIn("^FD>:A>0B^FS", out)

    def test_qr_shift(self):
        out = zpl.label_zpl(Label(elements=[QRElement(x=10, y=50, data="x", error_correction="H")]))
        self.assertIn("^FO10,40^BQN,2,4^FH_^FDHA,x^FS", out)

    def test_filled_box(self):
        out = zpl.label_zpl(Label(elements=[BoxElement(x=0, y=0, w=100, h=40, filled=True)]))
        self.assertIn("^GB100,40,40,B,0^FS", out)

    def test_image(self):
        el = ImageElement(png_b64=base64.b64encode(PNG_2X2).decode(), w=2, h=2)
        self.assertEqual(zpl.image_to_gf(el), "^GFA,2,2,1,8040")

    def test_batch(self):
        lab = Label(elements=[TextElement(text="{n}")])
        out = zpl.batch_zpl(lab, [{"n": "1"}, {"n": "2"}])
        self.assertEqual(out.count("^XA"), 2)
        self.assertIn("^FD1^FS", out)
        self.assertIn("^FD2^FS", out)


class RenameTests(unittest.TestCase):
    def test_files_saved_as_labelix_still_open(self):
        old = Label(elements=[TextElement(text="Hi")]).to_json().replace(
            '"format": "zplinux"', '"format": "labelix"')
        self.assertEqual(Label.from_json(old).elements[0].text, "Hi")
        with self.assertRaises(ValueError):
            Label.from_json('{"format": "something else"}')


class AutoFieldTests(unittest.TestCase):
    def lab(self, text, **kw):
        return Label(elements=[TextElement(text=text)], **kw)

    def test_date_and_time(self):
        import datetime
        lab = self.lab("{date} {time}", date_format="%Y-%m-%d", time_format="%H:%M")
        when = datetime.datetime(2026, 2, 11, 9, 40, 25)
        self.assertEqual(lab.auto_values(now=when)["date"], "2026-02-11")
        self.assertEqual(lab.auto_values(now=when)["time"], "09:40")
        self.assertEqual(lab.manual_variables(), [])

    def test_counter_padding_and_step(self):
        lab = self.lab("#{counter}", counter_next=7, counter_step=5, counter_digits=4)
        self.assertEqual(lab.filled({}).elements[0].text, "#0007")
        self.assertEqual(lab.filled({}, offset=2).elements[0].text, "#0017")

    def test_each_copy_gets_its_own_number(self):
        out = zpl.label_zpl(self.lab("#{counter}", counter_next=7, copies=3))
        self.assertEqual(out.count("^XA"), 3)
        for n in (7, 8, 9):
            self.assertIn(f"^FD#{n}^FS", out)
        self.assertNotIn("^PQ", out)

    def test_copies_without_a_counter_use_pq(self):
        out = zpl.label_zpl(self.lab("Hi", copies=3))
        self.assertEqual(out.count("^XA"), 1)
        self.assertIn("^PQ3", out)

    def test_batch_carries_the_counter_on(self):
        lab = self.lab("{name} #{counter}", counter_next=1, copies=2)
        out = zpl.batch_zpl(lab, [{"name": "A"}, {"name": "B"}])
        for text in ("A #1", "A #2", "B #3", "B #4"):
            self.assertIn(f"^FD{text}^FS", out)
        self.assertEqual(zpl.labels_printed(lab, 2), 4)
        self.assertEqual(lab.manual_variables(), ["name"])

    def test_settings_are_saved(self):
        lab = self.lab("x", date_format="%m/%d/%Y", counter_next=42, counter_digits=3)
        back = Label.from_json(lab.to_json())
        self.assertEqual((back.date_format, back.counter_next, back.counter_digits),
                         ("%m/%d/%Y", 42, 3))


class RotationTests(unittest.TestCase):
    def test_rotate_rect(self):
        # a 10 x 4 rect at the top-left of a 100 x 50 area
        r = (0, 0, 10, 4)
        self.assertEqual(rotate_rect(r, 100, 50, 90), (46, 0, 4, 10))
        self.assertEqual(rotate_rect(r, 100, 50, 180), (90, 46, 10, 4))
        self.assertEqual(rotate_rect(r, 100, 50, 270), (0, 90, 4, 10))

    def test_bounds_swap_at_90(self):
        el = BoxElement(w=60, h=20)
        img = ImageElement(w=60, h=20, rotation=90)
        self.assertEqual(el.bounds()[2:], (60, 20))
        self.assertEqual(img.bounds()[2:], (20, 60))

    def test_rotated_fields(self):
        lab = Label(width_mm=101.6, height_mm=101.6, elements=[
            TextElement(x=40, y=40, text="Hi", height=40, rotation=90),
            Code128Element(x=40, y=300, data="12345678", height=60, module=2, rotation=90)])
        out = zpl.label_zpl(lab)
        self.assertIn("^FO40,40^A0R,40,40", out)
        # ^FO is the corner of the bars; the text below them lies to their left at 90
        bc = lab.filled({}).elements[1]
        from zplinux import render
        render.measure(Label(elements=[bc]))
        text_space = bc.size()[1] - bc.height
        self.assertIn(f"^FO{40 + text_space},300^BCR,60,Y", out)

    def landscape(self, orientation=0):
        # a 6 x 4 in design
        return Label(width_mm=152.4, height_mm=101.6, orientation=orientation, elements=[
            TextElement(x=30, y=20, text="Blue Mug", height=50),
            BoxElement(x=10, y=100, w=300, h=40, thickness=4),
            QRElement(x=900, y=40, data="x", magnification=5)])

    def test_rotate_when_printing(self):
        out = zpl.label_zpl(self.landscape(90))
        # prints on 4 x 6 in stock: 813 dots wide, 1219 long
        self.assertIn("^PW813\n^LL1219", out)
        # text at (30, 20), 50 high, turned 90 clockwise: x = 813 - 20 - 50
        self.assertIn("^FO743,30^A0R,50,50", out)
        # the box turns by swapping its sides
        self.assertIn("^GB40,300,4", out)
        self.assertNotIn("^PW1219", out)

    def test_rotating_twice_the_other_way_matches(self):
        self.assertEqual(zpl.label_zpl(self.landscape(0)).count("^A0N"), 1)
        out90, out270 = zpl.label_zpl(self.landscape(90)), zpl.label_zpl(self.landscape(270))
        self.assertIn("^A0R", out90)
        self.assertIn("^A0B", out270)
        self.assertIn("^A0I", zpl.label_zpl(self.landscape(180)))

    def test_rotating_when_printing_leaves_the_design_alone(self):
        lab = self.landscape(90)
        before = lab.to_json()
        zpl.label_zpl(lab)
        self.assertEqual(lab.to_json(), before)
        self.assertEqual(lab.stock_mm, (101.6, 152.4))

    def test_orientation_is_saved(self):
        lab = Label(orientation=90)
        self.assertEqual(Label.from_json(lab.to_json()).orientation, 90)


class OffLabelTests(unittest.TestCase):
    """Objects may sit partly or wholly off the label; only what is on it prints."""

    def lab(self, *els):
        return Label(width_mm=50.8, height_mm=25.4, dpi=203, elements=list(els))   # 406 x 203

    def fields(self, *els):
        return [l for l in zpl.label_zpl(self.lab(*els)).splitlines() if l.startswith("^FO")
                or l.startswith("^BY")]

    def test_wholly_off_the_label_is_left_out(self):
        self.assertEqual(self.fields(BoxElement(x=500, y=10, w=50, h=50)), [])
        self.assertEqual(self.fields(BoxElement(x=-80, y=10, w=50, h=50)), [])
        self.assertEqual(self.fields(BoxElement(x=10, y=-80, w=50, h=50)), [])

    def test_past_the_right_or_bottom_edge_prints_normally(self):
        # the printer cuts fields off at the label's edge itself
        self.assertEqual(self.fields(BoxElement(x=380, y=180, w=50, h=50, thickness=4)),
                         ["^FO380,180^GB50,50,4,B,0^FS"])

    def test_past_the_left_edge_becomes_a_graphic_of_the_visible_part(self):
        out = self.fields(BoxElement(x=-20, y=30, w=50, h=40, filled=True))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("^FO0,30^GFA,"), out[0])
        total, _, row_bytes = out[0].split("^GFA,")[1].split(",")[:3]
        self.assertEqual((int(row_bytes), int(total) // int(row_bytes)), (4, 40))  # 30 dots

    def test_reverse_text_keeps_reversing(self):
        out = self.fields(TextElement(x=-10, y=-5, text="HI", height=40, reverse=True))
        self.assertTrue(out[0].startswith("^FO0,0^FR^GFA,"), out[0])

    def test_placement(self):
        lab = self.lab()
        self.assertEqual(zpl.placement(BoxElement(x=10, y=10, w=50, h=50), lab), "inside")
        self.assertEqual(zpl.placement(BoxElement(x=380, y=10, w=50, h=50), lab), "cut")
        self.assertEqual(zpl.placement(BoxElement(x=-60, y=10, w=50, h=50), lab), "outside")


class BarcodeTests(unittest.TestCase):
    def test_code128_width(self):
        widths = barcode.code128("PJJ123C")       # no digit run long enough for subset C
        self.assertEqual(sum(widths), barcode.code128_modules("PJJ123C"))
        self.assertIsNone(barcode.code128("é"))

    def test_code128_checksum(self):
        # Start B 104 + 1*48 + 2*42 + 3*42 + 4*17 + 5*18 + 6*19 + 7*35 = 879; 879 % 103 = 55
        widths = barcode.code128("PJJ123C")
        check = widths[-13:-7]
        self.assertEqual("".join(map(str, check)), barcode._C128[55])

    def test_code128_subset_c_like_zebradesigner(self):
        # ZebraDesigner writes 12669 as >;1266>69: start C for 12 66, then B for the odd 9.
        self.assertEqual(barcode.code128_zpl_data("12669"), ">;1266>69")
        # Start C 105 + 1*12 + 2*66 + 3*100 (code B) + 4*25 ('9') = 649; 649 % 103 = 31
        self.assertEqual(barcode.code128_values("12669"), [105, 12, 66, 100, 25, 31, 106])
        self.assertLess(sum(barcode.code128("12669")), barcode.code128_modules("12669"))

    def test_code128_subset_rules(self):
        cases = {
            "1234": ">;1234",              # 4+ leading digits start in C
            "123": ">:123",                # 3 digits are no shorter in C
            "12": ">;12",                  # exactly two digits
            "MUG-0001": ">:MUG->50001",    # 4 digits at the end
            "A1234B": ">:A1234B",          # 4 in the middle save nothing
            "A12345678B": ">:A>512345678>6B",
            "ABC-12345": ">:ABC-1>52345",  # odd run: first digit stays in B
            "A>B": ">:A>0B",
        }
        for data, expected in cases.items():
            with self.subTest(data=data):
                self.assertEqual(barcode.code128_zpl_data(data), expected)
                widths = barcode.code128(data)
                self.assertEqual(len(widths) % 6, 1)   # whole symbols plus the stop bar


class PrintingTests(unittest.TestCase):
    def test_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.zpl")
            printing.send("^XA^XZ", printing.PrinterConfig(method="file", file_path=path))
            with open(path) as f:
                self.assertEqual(f.read(), "^XA^XZ")

    def test_network_via_fake_printer(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "tools", "fake_printer.py")
            proc = subprocess.Popen([sys.executable, script, "--port", str(port), "--out", d],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                cfg = printing.PrinterConfig(method="network", host="127.0.0.1", port=port)
                for _ in range(50):
                    try:
                        printing.send("^XA^FDtest^FS^XZ", cfg)
                        break
                    except printing.PrintError:
                        time.sleep(0.1)
                for _ in range(50):
                    files = [f for f in os.listdir(d) if f.endswith(".zpl")]
                    if files:
                        break
                    time.sleep(0.1)
                self.assertEqual(len(files), 1)
                with open(os.path.join(d, files[0])) as f:
                    self.assertEqual(f.read(), "^XA^FDtest^FS^XZ")
            finally:
                proc.terminate()
                proc.wait()

    def test_network_unreachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(printing.PrintError):
            printing.send("^XA^XZ", printing.PrinterConfig(method="network", port=port), timeout=1)

    def test_connection_test(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            msg = printing.test(printing.PrinterConfig(method="network", port=port))
            self.assertIn("reachable", msg)
        with self.assertRaises(printing.PrintError):
            printing.test(printing.PrinterConfig(method="network", port=port), timeout=1)

    def test_missing_device(self):
        with self.assertRaises(printing.PrintError):
            printing.send("^XA^XZ", printing.PrinterConfig(method="device", device="/nonexistent/lp9"))


class SavedPrinterTests(unittest.TestCase):
    def test_nothing_saved_gives_local_test_printer(self):
        printers, default = printing.load_printers({})
        self.assertEqual(len(printers), 1)
        self.assertEqual(default, 0)
        self.assertEqual(printers[0].host, "127.0.0.1")
        self.assertTrue(printers[0].name)

    def test_old_single_printer_is_migrated(self):
        settings = {"printer": {"method": "device", "device": "/dev/usb/lp1"}}
        printers, default = printing.load_printers(settings)
        self.assertEqual([p.device for p in printers], ["/dev/usb/lp1"])
        self.assertEqual(default, 0)
        printing.store_printers(settings, printers, default)
        self.assertNotIn("printer", settings)
        self.assertEqual(settings["default_printer"], printers[0].name)

    def test_round_trip_keeps_names_and_default(self):
        settings = {}
        printers = [printing.PrinterConfig(name="Office", host="10.0.0.5"),
                    printing.PrinterConfig(name="Warehouse GK420d", method="cups",
                                           cups_queue="GK420d")]
        printing.store_printers(settings, printers, 1)
        loaded, default = printing.load_printers(settings)
        self.assertEqual([p.name for p in loaded], ["Office", "Warehouse GK420d"])
        self.assertEqual(default, 1)
        self.assertEqual(loaded[default].cups_queue, "GK420d")

    def test_missing_default_falls_back_to_first(self):
        settings = {"printers": [{"name": "A"}, {"name": "B"}], "default_printer": "gone"}
        self.assertEqual(printing.load_printers(settings)[1], 0)

    def test_names_are_made_unique(self):
        printers = [printing.PrinterConfig(name="Zebra"), printing.PrinterConfig(name="Zebra"),
                    printing.PrinterConfig(name="  ")]
        printing.unique_names(printers)
        self.assertEqual([p.name for p in printers], ["Zebra", "Zebra (2)", "Printer 3"])


if __name__ == "__main__":
    unittest.main()
