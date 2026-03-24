from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
import re
from typing import Dict, List

from .csv_store import ensure_csv, read_all, write_all_atomic

CASH_DETAIL_HEADERS = [
    "fecha",
    "tipo",
    "descripcion",
    "importe",
    "saldo",
    "referencia",
    "usuario",
]

CASH_DAILY_HEADERS = [
    "fecha",
    "saldo_final",
    "movimientos",
]

_ID_TIMESTAMP_RE = re.compile(r"^[A-Z]{2,3}-(\d{14})")
_ADJUSTMENT_RE = re.compile(
    r"Saldo\s+(-?\d+(?:[.,]\d+)?)\s*->\s*(-?\d+(?:[.,]\d+)?)\.\s*Nota:\s*(.*)$",
    re.IGNORECASE,
)


def _to_float(raw: object) -> float:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        return 0.0
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return 0.0


def _round2(value: float) -> float:
    rounded = round(float(value), 2)
    if abs(rounded) < 1e-9:
        return 0.0
    return rounded


def _parse_iso_datetime(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_amount(value: float) -> str:
    return f"{_round2(value):.2f}"


def _id_time_value(reference: str) -> time | None:
    match = _ID_TIMESTAMP_RE.match((reference or "").strip())
    if not match:
        return None
    raw_ts = match.group(1)
    try:
        return datetime.strptime(raw_ts, "%Y%m%d%H%M%S").time()
    except ValueError:
        return None


def _effective_event_datetime(raw: str, reference: str = "") -> datetime | None:
    parsed = _parse_iso_datetime(raw)
    if not parsed:
        return None
    if parsed.time() != time(0, 0, 0):
        return parsed
    id_time = _id_time_value(reference)
    if not id_time:
        return parsed
    return datetime.combine(parsed.date(), id_time)


def _extract_note_value(note: str, prefix: str) -> str:
    for part in (note or "").split("|"):
        text = part.strip()
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def _extract_note_text(note: str) -> str:
    for part in (note or "").split("|"):
        text = part.strip()
        if not text:
            continue
        if text.startswith("Usr:") or text.startswith("Prov:") or text.startswith("€/u:"):
            continue
        return text
    return ""


def _event_priority(kind: str) -> int:
    priorities = {
        "factura": 10,
        "compra": 20,
        "gasto": 30,
        "ajuste": 40,
    }
    return priorities.get(kind, 99)


def _invoice_events(facturas_csv: Path) -> List[Dict[str, object]]:
    events: List[Dict[str, object]] = []
    for row in read_all(facturas_csv):
        reference = (row.get("factura_id") or "").strip()
        occurred_at = _effective_event_datetime(row.get("fecha", ""), reference)
        if not occurred_at:
            continue

        amount = _round2(_to_float(row.get("total_importe", "0")))
        if abs(amount) < 1e-9:
            continue

        events.append({
            "fecha": occurred_at,
            "tipo": "factura",
            "descripcion": (row.get("nota_global") or "").strip(),
            "importe": amount,
            "referencia": reference,
            "usuario": (row.get("usuario") or "").strip(),
        })
    return events


def _purchase_events(movs_csv: Path) -> List[Dict[str, object]]:
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for row in read_all(movs_csv):
        if (row.get("origen") or "").strip().upper() != "COMPRA":
            continue
        if (row.get("tipo") or "").strip().upper() not in {"ENTRADA", "INFO"}:
            continue
        reference = (row.get("ref_id") or "").strip()
        if not reference:
            continue
        grouped.setdefault(reference, []).append(row)

    events: List[Dict[str, object]] = []
    for reference, items in grouped.items():
        items.sort(key=lambda row: row.get("fecha", ""))
        first = items[0]
        occurred_at = _effective_event_datetime(first.get("fecha", ""), reference)
        if not occurred_at:
            continue

        total = 0.0
        descriptions: List[str] = []
        user = _extract_note_value(first.get("nota", ""), "Usr:")
        for item in items:
            note = item.get("nota", "")
            qty = _to_float(item.get("cantidad", "0"))
            unit_price = _to_float(_extract_note_value(note, "€/u:"))
            total += qty * unit_price

            description = _extract_note_text(note)
            if description:
                descriptions.append(description)

        amount = _round2(-total)
        if abs(amount) < 1e-9:
            continue

        events.append({
            "fecha": occurred_at,
            "tipo": "compra",
            "descripcion": descriptions[0] if descriptions else "",
            "importe": amount,
            "referencia": reference,
            "usuario": user,
        })
    return events


def _expense_events(expenses_csv: Path) -> List[Dict[str, object]]:
    events: List[Dict[str, object]] = []
    for row in read_all(expenses_csv):
        reference = (row.get("gasto_id") or "").strip()
        occurred_at = _effective_event_datetime(row.get("fecha", ""), reference)
        if not occurred_at:
            continue

        amount = _round2(-_to_float(row.get("importe", "0")))
        if abs(amount) < 1e-9:
            continue

        events.append({
            "fecha": occurred_at,
            "tipo": "gasto",
            "descripcion": (row.get("descripcion") or "").strip(),
            "importe": amount,
            "referencia": reference,
            "usuario": (row.get("usuario") or "").strip(),
        })
    return events


def _parse_adjustment(detail: str) -> tuple[float, float, str] | None:
    match = _ADJUSTMENT_RE.match((detail or "").strip())
    if not match:
        return None
    previous = _to_float(match.group(1))
    current = _to_float(match.group(2))
    note = (match.group(3) or "").strip()
    return previous, current, note


def _adjustment_events(logs_csv: Path) -> List[Dict[str, object]]:
    events: List[Dict[str, object]] = []
    for row in read_all(logs_csv):
        if (row.get("accion") or "").strip().upper() != "AJUSTE CAJA":
            continue

        parsed = _parse_adjustment(row.get("detalle", ""))
        occurred_at = _parse_iso_datetime(row.get("fecha", ""))
        if not parsed or not occurred_at:
            continue

        previous, current, note = parsed
        amount = _round2(current - previous)
        if abs(amount) < 1e-9:
            continue

        clean_note = note if note and note != "-" else "Ajuste manual de caja"
        events.append({
            "fecha": occurred_at,
            "tipo": "ajuste",
            "descripcion": clean_note,
            "importe": amount,
            "referencia": (row.get("log_id") or "").strip(),
            "usuario": (row.get("usuario") or "").strip(),
        })
    return events


def _empty_daily_rows(current_balance: float) -> List[Dict[str, str]]:
    today = date.today().isoformat()
    return [{
        "fecha": today,
        "saldo_final": _format_amount(current_balance),
        "movimientos": "0",
    }]


def rebuild_cash_history(
    *,
    detail_csv: Path,
    daily_csv: Path,
    facturas_csv: Path,
    movs_csv: Path,
    expenses_csv: Path,
    logs_csv: Path,
    current_balance: float,
) -> None:
    ensure_csv(detail_csv, CASH_DETAIL_HEADERS)
    ensure_csv(daily_csv, CASH_DAILY_HEADERS)

    events = [
        *_invoice_events(facturas_csv),
        *_purchase_events(movs_csv),
        *_expense_events(expenses_csv),
        *_adjustment_events(logs_csv),
    ]
    events.sort(
        key=lambda item: (
            item["fecha"],
            _event_priority(str(item.get("tipo", ""))),
            str(item.get("referencia", "")),
            str(item.get("descripcion", "")),
        )
    )

    if not events:
        write_all_atomic(detail_csv, [], CASH_DETAIL_HEADERS)
        write_all_atomic(daily_csv, _empty_daily_rows(_round2(current_balance)), CASH_DAILY_HEADERS)
        return

    running_balance = _round2(current_balance - sum(float(item.get("importe", 0)) for item in events))
    opening_balance = running_balance
    detail_rows: List[Dict[str, str]] = []
    closing_by_day: Dict[date, float] = {}
    movements_by_day: Dict[date, int] = defaultdict(int)

    for item in events:
        running_balance = _round2(running_balance + float(item.get("importe", 0)))
        occurred_at = item["fecha"]
        balance_day = occurred_at.date()
        closing_by_day[balance_day] = running_balance
        movements_by_day[balance_day] += 1
        detail_rows.append({
            "fecha": _format_timestamp(occurred_at),
            "tipo": str(item.get("tipo", "")).strip(),
            "descripcion": str(item.get("descripcion", "")).strip(),
            "importe": _format_amount(float(item.get("importe", 0))),
            "saldo": _format_amount(running_balance),
            "referencia": str(item.get("referencia", "")).strip(),
            "usuario": str(item.get("usuario", "")).strip(),
        })

    first_day = events[0]["fecha"].date()
    last_day = max(events[-1]["fecha"].date(), date.today())
    seed_day = first_day - timedelta(days=1)
    current_day = seed_day
    current_closing = opening_balance
    daily_rows: List[Dict[str, str]] = []

    while current_day <= last_day:
        if current_day != seed_day:
            current_closing = _round2(closing_by_day.get(current_day, current_closing))
        daily_rows.append({
            "fecha": current_day.isoformat(),
            "saldo_final": _format_amount(current_closing),
            "movimientos": str(movements_by_day.get(current_day, 0)),
        })
        current_day += timedelta(days=1)

    write_all_atomic(detail_csv, detail_rows, CASH_DETAIL_HEADERS)
    write_all_atomic(daily_csv, daily_rows, CASH_DAILY_HEADERS)


def _balance_on_or_before(daily_rows: List[Dict[str, str]], target_day: date) -> float:
    latest = None
    first_valid = None
    for row in daily_rows:
        try:
            row_day = date.fromisoformat((row.get("fecha") or "").strip())
        except ValueError:
            continue
        if first_valid is None:
            first_valid = row
        if row_day <= target_day:
            latest = row
        else:
            break
    if latest is not None:
        return _to_float(latest.get("saldo_final", "0"))
    if first_valid is not None:
        return _to_float(first_valid.get("saldo_final", "0"))
    return 0.0


def get_cash_detail_report(
    *,
    detail_csv: Path,
    daily_csv: Path,
    start_date: date,
    end_date: date,
) -> Dict[str, object]:
    detail_rows = read_all(detail_csv)
    daily_rows = read_all(daily_csv)

    rows: List[Dict[str, object]] = []
    for row in detail_rows:
        parsed = _parse_iso_datetime(row.get("fecha", ""))
        if not parsed:
            continue
        if parsed.date() < start_date or parsed.date() > end_date:
            continue

        amount = _round2(_to_float(row.get("importe", "0")))
        balance = _round2(_to_float(row.get("saldo", "0")))
        rows.append({
            "fecha": row.get("fecha", ""),
            "fecha_label": parsed.date().isoformat(),
            "tipo": (row.get("tipo") or "").strip(),
            "descripcion": (row.get("descripcion") or "").strip(),
            "importe": row.get("importe", "0.00"),
            "importe_valor": amount,
            "saldo": row.get("saldo", "0.00"),
            "saldo_valor": balance,
            "referencia": (row.get("referencia") or "").strip(),
            "usuario": (row.get("usuario") or "").strip(),
            "importe_clase": "positive" if amount > 0 else "negative" if amount < 0 else "neutral",
        })

    opening_balance = _round2(_balance_on_or_before(daily_rows, start_date - timedelta(days=1)))
    closing_balance = (
        _round2(rows[-1]["saldo_valor"])
        if rows
        else _round2(_balance_on_or_before(daily_rows, end_date))
    )

    total_delta = _round2(sum(float(row["importe_valor"]) for row in rows))
    positive_total = _round2(sum(float(row["importe_valor"]) for row in rows if float(row["importe_valor"]) > 0))
    negative_total = _round2(sum(float(row["importe_valor"]) for row in rows if float(row["importe_valor"]) < 0))
    type_counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        type_counts[str(row.get("tipo", ""))] += 1

    return {
        "rows": rows,
        "count": len(rows),
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "net_change": total_delta,
        "positive_total": positive_total,
        "negative_total": negative_total,
        "type_counts": dict(type_counts),
    }
