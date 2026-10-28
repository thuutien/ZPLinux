"""Tests for reading batch files. Run: python3 -m unittest discover tests

The spreadsheets are built here the way Excel and LibreOffice write them."""

import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zplinux import datafile  # noqa: E402

XLSX = {
    "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
    "xl/workbook.xml": '''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Orders" sheetId="1" r:id="rId1"/><sheet name="Other" sheetId="2" r:id="rId2"/></sheets>
</workbook>''',
    "xl/_rels/workbook.xml.rels": '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId2" Type="worksheet" Target="worksheets/sheet1.xml"/>
 <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>''',
    "xl/sharedStrings.xml": '''<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="5" uniqueCount="5">
 <si><t>name</t></si><si><t>sku</t></si><si><t>shipped</t></si>
 <si><r><t>Blue </t></r><r><rPr><b/></rPr><t>Mug</t></r></si><si><t>MUG-0001</t></si>
</sst>''',
    "xl/styles.xml": '''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <numFmts count="1"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>
 <cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="14"/></cellXfs>
</styleSheet>''',
    # the first sheet in workbook order is sheet2.xml (rId1)
    "xl/worksheets/sheet2.xml": '''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
 <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c>
   <c r="D1" t="inlineStr"><is><t>barcode</t></is></c></row>
 <row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c><c r="C2" s="1"><v>46064</v></c>
   <c r="D2"><v>590123412345</v></c></row>
 <row r="4"><c r="A4" t="inlineStr"><is><t>Red Cup</t></is></c><c r="C4" s="2"><v>46065.5</v></c>
   <c r="D4"><v>12.5</v></c></row>
</sheetData></worksheet>''',
    "xl/worksheets/sheet1.xml": '''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
 <row r="1"><c r="A1" t="inlineStr"><is><t>wrong sheet</t></is></c></row>
</sheetData></worksheet>''',
}

ODS_CONTENT = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
<office:body><office:spreadsheet>
<table:table table:name="Orders">
 <table:table-column table:number-columns-repeated="4"/>
 <table:table-row>
  <table:table-cell office:value-type="string"><text:p>name</text:p></table:table-cell>
  <table:table-cell office:value-type="string"><text:p>sku</text:p></table:table-cell>
  <table:table-cell office:value-type="string"><text:p>shipped</text:p></table:table-cell>
  <table:table-cell office:value-type="string"><text:p>barcode</text:p></table:table-cell>
  <table:table-cell table:number-columns-repeated="1020"/>
 </table:table-row>
 <table:table-row>
  <table:table-cell office:value-type="string"><text:p>Blue<text:s/>Mug</text:p></table:table-cell>
  <table:table-cell office:value-type="string"><text:p>MUG-0001</text:p></table:table-cell>
  <table:table-cell office:value-type="date" office:date-value="2026-02-11"><text:p>11/02/26</text:p></table:table-cell>
  <table:table-cell office:value-type="float" office:value="590123412345"><text:p>590,123,412,345</text:p></table:table-cell>
 </table:table-row>
 <table:table-row table:number-rows-repeated="3"><table:table-cell table:number-columns-repeated="4"/></table:table-row>
 <table:table-row>
  <table:table-cell table:number-columns-repeated="2" office:value-type="string"><text:p>x</text:p></table:table-cell>
 </table:table-row>
 <table:table-row table:number-rows-repeated="1048000"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>
</table:table>
</office:spreadsheet></office:body></office:document-content>'''


class DataFileTests(unittest.TestCase):
    def write_zip(self, name, files):
        path = os.path.join(self.dir.name, name)
        with zipfile.ZipFile(path, "w") as z:
            for n, text in files.items():
                z.writestr(n, text)
        return path

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_xlsx(self):
        rows = datafile.read_rows(self.write_zip("orders.xlsx", XLSX))
        self.assertEqual(rows, [
            {"name": "Blue Mug", "sku": "MUG-0001", "shipped": "2026-02-11",
             "barcode": "590123412345"},
            {"name": "Red Cup", "sku": "", "shipped": "2026-02-12 12:00", "barcode": "12.5"}])

    def test_ods(self):
        rows = datafile.read_rows(self.write_zip("orders.ods",
                                                 {"mimetype": "application/vnd.oasis.opendocument.spreadsheet",
                                                  "content.xml": ODS_CONTENT}))
        self.assertEqual(rows, [
            {"name": "Blue Mug", "sku": "MUG-0001", "shipped": "2026-02-11",
             "barcode": "590123412345"},
            {"name": "x", "sku": "x", "shipped": "", "barcode": ""}])

    def test_csv_with_semicolons(self):
        path = os.path.join(self.dir.name, "orders.csv")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("name;sku\nBlue Mug;MUG-0001\nRed Cup;CUP-0002\n")
        self.assertEqual([r["sku"] for r in datafile.read_rows(path)], ["MUG-0001", "CUP-0002"])

    def test_leading_zeros_stay_in_text_cells(self):
        files = dict(XLSX)
        files["xl/sharedStrings.xml"] = files["xl/sharedStrings.xml"].replace(
            "<t>MUG-0001</t>", "<t>00123</t>")
        rows = datafile.read_rows(self.write_zip("zeros.xlsx", files))
        self.assertEqual(rows[0]["sku"], "00123")

    def test_broken_file(self):
        path = os.path.join(self.dir.name, "broken.xlsx")
        with open(path, "wb") as f:
            f.write(b"not a zip")
        with self.assertRaises(datafile.DataFileError):
            datafile.read_rows(path)


if __name__ == "__main__":
    unittest.main()
