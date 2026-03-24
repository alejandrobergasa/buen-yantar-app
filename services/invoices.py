from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from .csv_store import read_all, append_rows
from .ids import prefixed_id, short_id
from .inventory import MOV_HEADERS, now_iso

INVOICE_HEADERS = [
    "factura_id",
    "fecha",
    "cliente",
    "nota_global",
    "total_importe",
    "lineas",
    "usuario",
]

INVOICE_LINE_HEADERS = [
    "linea_id",
    "factura_id",
    "producto_id",
    "producto_nombre",
    "unidad",
    "cantidad",
    "precio_unitario",
    "importe_linea",
    "nota",
]


def _format_qty(value: float) -> str:
    if abs(value - int(value)) < 1e-9:
        return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _invoice_ts(invoice_date: str) -> str:
    raw = (invoice_date or "").strip()
    if not raw:
        return now_iso()
    try:
        base_dt = datetime.strptime(raw, "%Y-%m-%d")
        return base_dt.strftime("%Y-%m-%d 00:00:00")
    except ValueError:
        return now_iso()


def create_invoice(
    facturas_csv: Path,
    facturas_lineas_csv: Path,
    movs_csv: Path,
    lines: List[Dict[str, str]],
    cliente: str = "",
    nota_global: str = "",
    invoice_date: str = "",
    user: str = "",
) -> Tuple[str, float]:
    factura_id = prefixed_id("FV")
    move_ts = _invoice_ts(invoice_date)

    cliente = (cliente or "").strip()
    nota_global = (nota_global or "").strip()
    user = (user or "").strip()

    total = 0.0
    line_rows: List[Dict[str, str]] = []
    mov_rows: List[Dict[str, str]] = []

    for line in lines:
        producto_id = (line.get("producto_id") or "").strip()
        producto_nombre = (line.get("producto_nombre") or "").strip()
        unidad = (line.get("unidad") or "ud").strip() or "ud"
        cantidad = float(line.get("cantidad") or 0)
        stock_cantidad = float(line.get("stock_cantidad") or cantidad)
        precio_unitario = float(line.get("precio_unitario") or 0)
        nota_linea = (line.get("nota") or "").strip()

        importe_linea = round(cantidad * precio_unitario, 2)
        total += importe_linea

        line_rows.append({
            "linea_id": short_id(),
            "factura_id": factura_id,
            "producto_id": producto_id,
            "producto_nombre": producto_nombre,
            "unidad": unidad,
            "cantidad": _format_qty(cantidad),
            "precio_unitario": f"{precio_unitario:.2f}",
            "importe_linea": f"{importe_linea:.2f}",
            "nota": nota_linea,
        })

        note_parts: List[str] = []
        if cliente:
            note_parts.append(f"Cliente: {cliente}")
        note_parts.append(f"€/u: {precio_unitario:.2f}")
        if nota_global:
            note_parts.append(nota_global)
        if nota_linea:
            note_parts.append(nota_linea)

        mov_rows.append({
            "mov_id": short_id(),
            "fecha": move_ts,
            "producto_id": producto_id,
            "tipo": "SALIDA",
            "cantidad": _format_qty(stock_cantidad),
            "origen": "FACTURA",
            "ref_id": factura_id,
            "nota": " | ".join(note_parts),
        })

    header_row = {
        "factura_id": factura_id,
        "fecha": move_ts,
        "cliente": cliente,
        "nota_global": nota_global,
        "total_importe": f"{round(total, 2):.2f}",
        "lineas": str(len(line_rows)),
        "usuario": user,
    }

    append_rows(facturas_csv, [header_row], INVOICE_HEADERS)
    append_rows(facturas_lineas_csv, line_rows, INVOICE_LINE_HEADERS)
    append_rows(movs_csv, mov_rows, MOV_HEADERS)

    return factura_id, round(total, 2)


def list_invoices(facturas_csv: Path, limit: int = 400) -> List[Dict[str, str]]:
    rows = read_all(facturas_csv)
    rows.sort(key=lambda x: x.get("fecha", ""), reverse=True)
    if limit <= 0:
        return rows
    return rows[:limit]


def list_invoice_lines(facturas_lineas_csv: Path) -> List[Dict[str, str]]:
    return read_all(facturas_lineas_csv)


def find_invoice(facturas_csv: Path, factura_id: str) -> Dict[str, str] | None:
    target = (factura_id or "").strip()
    if not target:
        return None

    for row in read_all(facturas_csv):
        if (row.get("factura_id") or "").strip() == target:
            return row
    return None


def list_invoice_lines_for(facturas_lineas_csv: Path, factura_id: str) -> List[Dict[str, str]]:
    target = (factura_id or "").strip()
    if not target:
        return []

    return [
        row for row in read_all(facturas_lineas_csv)
        if (row.get("factura_id") or "").strip() == target
    ]
