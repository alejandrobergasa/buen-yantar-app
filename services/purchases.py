from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from .csv_store import read_all
_PURCHASE_HISTORY_CACHE: Dict[tuple[str, int, int], Dict[str, object]] = {}


def _file_cache_key(path: Path) -> tuple[str, int, int]:
    resolved = str(path.resolve())
    if not path.exists():
        return (resolved, 0, 0)
    stat = path.stat()
    return (resolved, stat.st_mtime_ns, stat.st_size)


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


def _purchase_history_base(movs_csv: Path) -> Dict[str, object]:
    cache_key = _file_cache_key(movs_csv)
    cached = _PURCHASE_HISTORY_CACHE.get(cache_key)
    if cached is not None:
        return cached

    rows = [
        m for m in read_all(movs_csv)
        if (m.get("tipo") or "").strip().upper() in {"ENTRADA", "INFO"}
        and (m.get("origen") or "").strip().upper() == "COMPRA"
    ]

    grouped: Dict[str, List[Dict[str, str]]] = {}
    for m in rows:
        ref = (m.get("ref_id") or "").strip()
        if not ref:
            continue
        grouped.setdefault(ref, []).append(m)

    tickets: List[Dict[str, str]] = []
    lines_by_ref: Dict[str, List[Dict[str, str]]] = {}

    for ref_id, items in grouped.items():
        items.sort(key=lambda x: x.get("fecha", ""), reverse=True)
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
                "cantidad": m.get("cantidad", "0"),
                "precio_compra": f"{unit_price_val:.2f}" if unit_price else "",
                "importe_linea": f"{importe:.2f}" if unit_price else "",
                "nota": m.get("nota", ""),
            })

        lines_by_ref[ref_id] = lines
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
    _PURCHASE_HISTORY_CACHE.clear()
    _PURCHASE_HISTORY_CACHE[cache_key] = {
        "tickets": tickets,
        "lines_by_ref": lines_by_ref,
    }
    return _PURCHASE_HISTORY_CACHE[cache_key]


def list_purchase_history(
    movs_csv: Path,
    product_names: Dict[str, str],
    limit: int = 400,
) -> Tuple[List[Dict[str, str]], Dict[str, List[Dict[str, str]]]]:
    base = _purchase_history_base(movs_csv)
    base_tickets = base["tickets"]
    base_lines_by_ref = base["lines_by_ref"]

    if limit > 0:
        tickets = [dict(ticket) for ticket in base_tickets[:limit]]
    else:
        tickets = [dict(ticket) for ticket in base_tickets]

    allowed = {t["ref_id"] for t in tickets}
    lines_map = {
        ref_id: [
            {
                **line,
                "producto_nombre": product_names.get((line.get("producto_id") or "").strip(), (line.get("producto_id") or "").strip()),
            }
            for line in base_lines_by_ref.get(ref_id, [])
        ]
        for ref_id in allowed
        if ref_id in base_lines_by_ref
    }

    return tickets, lines_map
