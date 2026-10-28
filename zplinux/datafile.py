"""Rows for batch printing from CSV, Excel (.xlsx) or LibreOffice/OpenOffice (.ods)
files: one dict per row, keyed by the first row's column headers.

The spreadsheet formats are zip files of XML; only the first sheet is read, with
the standard library, so no extra packages are needed.
"""

from __future__ import annotations

import csv
import datetime
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

EXTENSIONS = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".ods")

_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"


class DataFileError(Exception):
    pass


def read_rows(path: str) -> List[Dict[str, str]]:
    """Rows of the file, skipping empty ones. Raises DataFileError."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            table = _xlsx(path)
        elif ext == ".ods":
            table = _ods(path)
        else:
            return _csv(path)
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as e:
        raise DataFileError(f"Could not read {os.path.basename(path)}: {e}")
    return _to_dicts(table)


def _to_dicts(table: List[List[str]]) -> List[Dict[str, str]]:
    table = [row for row in table if any(c.strip() for c in row)]
    if not table:
        return []
    headers = [h.strip() for h in table[0]]
    while headers and not headers[-1]:
        headers.pop()
    rows = []
    for row in table[1:]:
        rows.append({h: (row[i] if i < len(row) else "") for i, h in enumerate(headers) if h})
    return rows


def _csv(path: str) -> List[Dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            return [{k: (v or "") for k, v in r.items() if k}
                    for r in csv.DictReader(fh, dialect=dialect)]
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise DataFileError(f"Could not read {os.path.basename(path)}: {e}")


# ---- Excel ------------------------------------------------------------------

# Built-in number formats that show dates (and times).
_DATE_FORMAT_IDS = set(range(14, 23)) | {45, 46, 47}


def _col_index(ref: str) -> int:
    """'C7' -> 2."""
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _number(text: str, is_date: bool) -> str:
    value = float(text)
    if is_date:
        when = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=value)
        return when.strftime("%Y-%m-%d") if value == int(value) else \
            when.strftime("%Y-%m-%d %H:%M")
    return str(int(value)) if value == int(value) and abs(value) < 1e15 else text


def _xlsx(path: str) -> List[List[str]]:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_X + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_X + "t")))
        date_styles = set()
        if "xl/styles.xml" in names:
            styles = ET.fromstring(z.read("xl/styles.xml"))
            custom = {int(f.get("numFmtId")): f.get("formatCode", "")
                      for f in styles.iter(_X + "numFmt")}
            xfs = styles.find(_X + "cellXfs")
            for i, xf in enumerate(xfs if xfs is not None else []):
                fid = int(xf.get("numFmtId", 0))
                code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom.get(fid, "")).lower()
                if fid in _DATE_FORMAT_IDS or (fid in custom and re.search(r"[dy]", code)):
                    date_styles.add(i)
        sheet = _first_sheet(z, names)
        root = ET.fromstring(z.read(sheet))
    table: List[List[str]] = []
    for row in root.iter(_X + "row"):
        cells: Dict[int, str] = {}
        for i, c in enumerate(row.iter(_X + "c")):
            col = _col_index(c.get("r")) if c.get("r") else i
            kind = c.get("t", "n")
            v = c.find(_X + "v")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(_X + "t"))
            elif v is None or v.text is None:
                text = ""
            elif kind == "s":
                text = shared[int(v.text)]
            elif kind == "b":
                text = "TRUE" if v.text == "1" else "FALSE"
            elif kind in ("str", "e"):
                text = v.text
            else:
                text = _number(v.text, int(c.get("s", 0)) in date_styles)
            cells[col] = text
        if cells:
            table.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return table


def _first_sheet(z: zipfile.ZipFile, names) -> str:
    """The first sheet in the workbook's order, following its relationship."""
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        first = next(wb.iter(_X + "sheet"))
        rid = first.get(_R + "id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.iter(_PKG + "Relationship")
                      if r.get("Id") == rid)
        target = target.lstrip("/")
        return target if target.startswith("xl/") else "xl/" + target
    except (KeyError, StopIteration):
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            raise KeyError("no worksheet")
        return sheets[0]


# ---- OpenDocument -----------------------------------------------------------

def _ods_text(cell) -> str:
    """The cell as displayed: its paragraphs, keeping spaces (text:s)."""
    lines = []
    for p in cell.iter(f"{{{_TEXT}}}p"):
        parts = []

        def walk(el):
            if el.tag == f"{{{_TEXT}}}s":
                parts.append(" " * int(el.get(f"{{{_TEXT}}}c", 1)))
            elif el.tag == f"{{{_TEXT}}}tab":
                parts.append("\t")
            elif el.text:
                parts.append(el.text)
            for child in el:
                walk(child)
                if child.tail:
                    parts.append(child.tail)
        walk(p)
        lines.append("".join(parts))
    return "\n".join(lines)


def _ods(path: str) -> List[List[str]]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    sheet = next(root.iter(f"{{{_TABLE}}}table"), None)
    if sheet is None:
        raise KeyError("no sheet")
    table: List[List[str]] = []
    for row in sheet.iter(f"{{{_TABLE}}}table-row"):
        values: List[str] = []
        for cell in row:
            if cell.tag not in (f"{{{_TABLE}}}table-cell", f"{{{_TABLE}}}covered-table-cell"):
                continue
            repeat = int(cell.get(f"{{{_TABLE}}}number-columns-repeated", 1))
            text = _ods_value(cell)
            values.extend([text] * min(repeat, 1024))
        while values and not values[-1]:
            values.pop()
        repeat = int(row.get(f"{{{_TABLE}}}number-rows-repeated", 1))
        table.extend([values] * (min(repeat, 1024) if values else 1))
    return table


def _ods_value(cell) -> str:
    """Plain numbers as stored (like Excel's, so 12345 doesn't become "12,345"), dates
    as YYYY-MM-DD, everything else as displayed."""
    kind = cell.get(f"{{{_OFFICE}}}value-type")
    if kind == "date":
        value = cell.get(f"{{{_OFFICE}}}date-value", "")
        if len(value) <= 10 or value.endswith("T00:00:00"):
            return value[:10]
        return value.replace("T", " ")[:16]
    if kind == "float":
        value: Optional[str] = cell.get(f"{{{_OFFICE}}}value")
        return _number(value, False) if value else _ods_text(cell)
    return _ods_text(cell)
