from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


XML_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def parse_xlsx_sheet_rows(path: Path, sheet_name: str) -> list[dict[str, str]]:
    if not path.exists():
        return []

    with zipfile.ZipFile(path) as zf:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", XML_NS):
                shared_strings.append("".join(text_node.text or "" for text_node in item.iterfind(".//main:t", XML_NS)))

        workbook_root = ET.fromstring(zf.read("xl/workbook.xml"))
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        relationship_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels_root}

        target = None
        for sheet in workbook_root.find("main:sheets", XML_NS):
            if sheet.attrib["name"] == sheet_name:
                rel_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
                target = relationship_map[rel_id]
                break
        if target is None:
            return []

        sheet_root = ET.fromstring(zf.read(f"xl/{target}"))
        sheet_data = sheet_root.find("main:sheetData", XML_NS)
        if sheet_data is None:
            return []

        raw_rows: list[list[str]] = []
        for row in sheet_data.findall("main:row", XML_NS):
            parsed_cells: list[str] = []
            for cell in row.findall("main:c", XML_NS):
                cell_type = cell.attrib.get("t")
                value_node = cell.find("main:v", XML_NS)
                if value_node is None:
                    parsed_cells.append("")
                    continue
                raw_value = value_node.text or ""
                parsed_cells.append(shared_strings[int(raw_value)] if cell_type == "s" else raw_value)
            if any(str(cell).strip() for cell in parsed_cells):
                raw_rows.append(parsed_cells)

    if not raw_rows:
        return []

    header = [str(cell).strip() for cell in raw_rows[0]]
    rows: list[dict[str, str]] = []
    for row in raw_rows[1:]:
        normalized = list(row) + [""] * (len(header) - len(row))
        rows.append({header[idx]: str(normalized[idx]).strip() for idx in range(len(header)) if header[idx]})
    return rows

