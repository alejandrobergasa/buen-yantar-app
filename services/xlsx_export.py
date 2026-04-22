from __future__ import annotations

from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


def _column_name(index: int) -> str:
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _inline_string_cell(ref: str, value: str, style: int = 0) -> str:
    return (
        f'<c r="{ref}" t="inlineStr" s="{style}">'
        f"<is><t>{escape(value)}</t></is>"
        "</c>"
    )


def _number_cell(ref: str, value: float, style: int) -> str:
    return f'<c r="{ref}" s="{style}"><v>{value:.2f}</v></c>'


def _build_workbook(
    *,
    sheet_rows: list[str],
    header_count: int,
    column_widths: list[int],
    sheet_name: str,
) -> bytes:
    row_count = max(len(sheet_rows), 1)
    cols_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(column_widths, start=1)
    )
    auto_filter_ref = f"A1:{_column_name(header_count)}{row_count}"
    worksheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
    </sheetView>
  </sheetViews>
  <cols>
    {cols_xml}
  </cols>
  <sheetData>
    {''.join(sheet_rows)}
  </sheetData>
  <autoFilter ref="{auto_filter_ref}"/>
</worksheet>
"""

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1">
    <numFmt numFmtId="164" formatCode="0.00"/>
  </numFmts>
  <fonts count="5">
    <font>
      <sz val="11"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <b/>
      <sz val="11"/>
      <color rgb="FF143730"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <sz val="11"/>
      <color rgb="FF1D5AA6"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <sz val="11"/>
      <color rgb="FFB0413E"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
    <font>
      <sz val="11"/>
      <color rgb="FF18342E"/>
      <name val="Calibri"/>
      <family val="2"/>
    </font>
  </fonts>
  <fills count="3">
    <fill>
      <patternFill patternType="none"/>
    </fill>
    <fill>
      <patternFill patternType="gray125"/>
    </fill>
    <fill>
      <patternFill patternType="solid">
        <fgColor rgb="FFE8F6F1"/>
        <bgColor indexed="64"/>
      </patternFill>
    </fill>
  </fills>
  <borders count="2">
    <border>
      <left/>
      <right/>
      <top/>
      <bottom/>
      <diagonal/>
    </border>
    <border>
      <left style="thin"><color rgb="FFD0E2DA"/></left>
      <right style="thin"><color rgb="FFD0E2DA"/></right>
      <top style="thin"><color rgb="FFD0E2DA"/></top>
      <bottom style="thin"><color rgb="FFD0E2DA"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="6">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center"/>
    </xf>
    <xf numFmtId="164" fontId="2" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>
    <xf numFmtId="164" fontId="3" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>
    <xf numFmtId="164" fontId="4" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>
    <xf numFmtId="164" fontId="4" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>
  </cellXfs>
  <cellStyles count="1">
    <cellStyle name="Normal" xfId="0" builtinId="0"/>
  </cellStyles>
</styleSheet>
"""

    workbook_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

    workbook_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

    root_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
"""

    core_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>Buen Yantar</dc:creator>
  <cp:lastModifiedBy>Buen Yantar</cp:lastModifiedBy>
</cp:coreProperties>
"""

    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Buen Yantar</Application>
</Properties>
"""

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", root_rels_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
    return buffer.getvalue()


def build_cash_detail_workbook(rows: list[dict[str, object]]) -> bytes:
    headers = ["fecha", "tipo", "descripcion", "importe", "saldo"]
    sheet_rows: list[str] = []

    header_cells = [
        _inline_string_cell(f"{_column_name(index)}1", header, style=1)
        for index, header in enumerate(headers, start=1)
    ]
    sheet_rows.append(f"<row r=\"1\">{''.join(header_cells)}</row>")

    for row_index, row in enumerate(rows, start=2):
        amount = float(row.get("importe_valor", row.get("importe", 0)) or 0)
        balance = float(row.get("saldo_valor", row.get("saldo", 0)) or 0)
        amount_style = 2 if amount > 0 else 3 if amount < 0 else 4
        cells = [
            _inline_string_cell(f"A{row_index}", str(row.get("fecha_label", row.get("fecha", "")))),
            _inline_string_cell(f"B{row_index}", str(row.get("tipo", ""))),
            _inline_string_cell(f"C{row_index}", str(row.get("descripcion", ""))),
            _number_cell(f"D{row_index}", amount, amount_style),
            _number_cell(f"E{row_index}", balance, 5),
        ]
        sheet_rows.append(f"<row r=\"{row_index}\">{''.join(cells)}</row>")

    return _build_workbook(
        sheet_rows=sheet_rows,
        header_count=len(headers),
        column_widths=[14, 16, 44, 14, 14],
        sheet_name="Tabla detalle",
    )


def build_cash_annual_workbook(
    rows: list[dict[str, object]],
    *,
    selected_year: int,
    opening_balance: float,
    closing_balance: float,
    net_balance: float,
) -> bytes:
    headers = ["mes", "total_ingresos", "total_gastos", "saldo"]
    sheet_rows: list[str] = []

    header_cells = [
        _inline_string_cell(f"{_column_name(index)}1", header, style=1)
        for index, header in enumerate(headers, start=1)
    ]
    sheet_rows.append(f"<row r=\"1\">{''.join(header_cells)}</row>")

    for row_index, row in enumerate(rows, start=2):
        income_total = float(row.get("income_total", 0) or 0)
        expense_total = float(row.get("expense_total", 0) or 0)
        balance = float(row.get("saldo", 0) or 0)
        balance_style = 2 if balance > 0 else 3 if balance < 0 else 4
        cells = [
            _inline_string_cell(f"A{row_index}", str(row.get("label", ""))),
            _number_cell(f"B{row_index}", income_total, 2),
            _number_cell(f"C{row_index}", -abs(expense_total), 3),
            _number_cell(f"D{row_index}", balance, balance_style),
        ]
        sheet_rows.append(f"<row r=\"{row_index}\">{''.join(cells)}</row>")

    summary_header_row = len(rows) + 3
    summary_header_cells = [
        _inline_string_cell(f"A{summary_header_row}", "Saldo inicial", style=1),
        _inline_string_cell(f"B{summary_header_row}", "Saldo final", style=1),
        _inline_string_cell(f"C{summary_header_row}", "Balance saldo caja", style=1),
    ]
    sheet_rows.append(f"<row r=\"{summary_header_row}\">{''.join(summary_header_cells)}</row>")

    summary_value_row = summary_header_row + 1
    summary_cells = [
        _number_cell(f"A{summary_value_row}", opening_balance, 5),
        _number_cell(f"B{summary_value_row}", closing_balance, 5),
        _number_cell(
            f"C{summary_value_row}",
            net_balance,
            2 if net_balance > 0 else 3 if net_balance < 0 else 4,
        ),
    ]
    sheet_rows.append(f"<row r=\"{summary_value_row}\">{''.join(summary_cells)}</row>")

    return _build_workbook(
        sheet_rows=sheet_rows,
        header_count=len(headers),
        column_widths=[18, 16, 16, 14],
        sheet_name=f"Resumen {selected_year}",
    )
