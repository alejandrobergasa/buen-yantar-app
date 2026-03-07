from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from .csv_store import read_all


def _to_float(raw: str) -> float:
    try:
        return float((raw or "").replace(",", "."))
    except ValueError:
        return 0.0


def _extract_from_note(note: str, prefix: str) -> str:
    for part in (note or "").split("|"):
        p = part.strip()
        if p.startswith(prefix):
            return p[len(prefix):].strip()
    return ""


def list_purchase_history(
    movs_csv: Path,
    product_names: Dict[str, str],
    limit: int = 400,
) -> Tuple[List[Dict[str, str]], Dict[str, List[Dict[str, str]]]]:
    rows = [
        m for m in read_all(movs_csv)
        if (m.get("tipo") or "").strip().upper() == "ENTRADA"
        and (m.get("origen") or "").strip().upper() == "COMPRA"
    ]
    rows.sort(key=lambda x: x.get("fecha", ""), reverse=True)

    grouped: Dict[str, List[Dict[str, str]]] = {}
    for m in rows:
        ref = (m.get("ref_id") or "").strip()
        if not ref:
            continue
        grouped.setdefault(ref, []).append(m)

    tickets: List[Dict[str, str]] = []
    lines_map: Dict[str, List[Dict[str, str]]] = {}

    for ref_id, items in grouped.items():
        first = items[0]
        usuario = _extract_from_note(first.get("nota", ""), "Usr:")
        proveedor = _extract_from_note(first.get("nota", ""), "Prov:")
        unidades = 0.0
        total = 0.0
        lines: List[Dict[str, str]] = []

        for m in items:
            qty = _to_float(m.get("cantidad", "0"))
            unidades += qty
            unit_price = _extract_from_note(m.get("nota", ""), "€/u:")
            unit_price_val = _to_float(unit_price) if unit_price else 0.0
            importe = qty * unit_price_val
            total += importe

            pid = (m.get("producto_id") or "").strip()
            lines.append({
                "producto_id": pid,
                "producto_nombre": product_names.get(pid, pid),
                "cantidad": m.get("cantidad", "0"),
                "precio_compra": f"{unit_price_val:.2f}" if unit_price else "",
                "importe_linea": f"{importe:.2f}" if unit_price else "",
                "nota": m.get("nota", ""),
            })

        lines_map[ref_id] = lines
        tickets.append({
            "ref_id": ref_id,
            "fecha": first.get("fecha", ""),
            "usuario": usuario,
            "proveedor": proveedor,
            "lineas": str(len(items)),
            "unidades": f"{unidades:.2f}".rstrip("0").rstrip("."),
            "total_importe": f"{total:.2f}",
        })

    tickets.sort(key=lambda x: x.get("fecha", ""), reverse=True)
    if limit > 0:
        tickets = tickets[:limit]
        allowed = {t["ref_id"] for t in tickets}
        lines_map = {k: v for k, v in lines_map.items() if k in allowed}

    return tickets, lines_map
