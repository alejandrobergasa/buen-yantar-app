from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Dict, List

from .csv_store import read_all, write_all_atomic
from .legacy_migration import LEGACY_INVOICE_PREFIX


CASH_MOVEMENT_HEADERS = [
    "movimiento_id",
    "fecha",
    "tipo",
    "descripcion",
    "importe",
    "saldo",
    "ref_id",
    "usuario",
    "origen",
]

CASH_DAILY_HEADERS = [
    "fecha",
    "saldo_cierre",
    "movimientos_dia",
    "actualizado_en",
]

_ADJUSTMENT_RE = re.compile(
    r"Saldo\s+(-?\d+(?:[.,]\d+)?)\s*->\s*(-?\d+(?:[.,]\d+)?)\.\s*Nota:\s*(.*)",
    re.IGNORECASE,
)

_TYPE_PRIORITY = {
    "ancla": 0,
    "factura": 1,
    "compra": 2,
    "gasto": 3,
    "ajuste": 4,
}
_MIGRATION_CASH_ACTION = "MIGRACION CAJA"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_float(raw: object) -> float:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        return 0.0
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return 0.0


def _format_money(value: float) -> str:
    return f"{round(float(value), 2):.2f}"


def _parse_datetime(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _sort_key(row: Dict[str, str]) -> tuple[datetime, int, str, str]:
    return (
        _parse_datetime(row.get("fecha", "")) or datetime.min,
        _TYPE_PRIORITY.get((row.get("tipo") or "").strip().lower(), 99),
        (row.get("ref_id") or "").strip(),
        (row.get("movimiento_id") or "").strip(),
    )


def _extract_note_value(note: str, prefix: str) -> str:
    for part in (note or "").split("|"):
        text = part.strip()
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def _collect_purchase_description(notes: List[str]) -> str:
    parts: List[str] = []
    seen: set[str] = set()
    for note in notes:
        for raw_part in (note or "").split("|"):
            text = raw_part.strip()
            if not text:
                continue
            if text.startswith("Usr:") or text.startswith("Prov:") or text.startswith("€/u:"):
                continue
            if text in seen:
                continue
            seen.add(text)
            parts.append(text)
    return " | ".join(parts)


def _current_cash_balance(cashbox_csv: Path) -> float:
    rows = read_all(cashbox_csv)
    row = rows[0] if rows else {}
    return round(_to_float(row.get("saldo_actual", "0")), 2)


def _invoice_movements(invoices_csv: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    legacy_prefix = f"{LEGACY_INVOICE_PREFIX}-"
    for invoice in read_all(invoices_csv):
        amount = round(_to_float(invoice.get("total_importe", "0")), 2)
        if abs(amount) < 1e-9:
            continue
        ref_id = (invoice.get("factura_id") or "").strip()
        rows.append({
            "movimiento_id": ref_id or f"factura-{len(rows) + 1}",
            "fecha": (invoice.get("fecha") or "").strip(),
            "tipo": "factura",
            "descripcion": (invoice.get("nota_global") or "").strip(),
            "importe_valor": amount,
            "ref_id": ref_id,
            "usuario": (invoice.get("usuario") or "").strip(),
            "origen": "facturas",
            "is_legacy": ref_id.upper().startswith(legacy_prefix),
        })
    return rows


def _purchase_movements(movs_csv: Path) -> List[Dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for movement in read_all(movs_csv):
        if (movement.get("origen") or "").strip().upper() != "COMPRA":
            continue
        if (movement.get("tipo") or "").strip().upper() not in {"ENTRADA", "INFO"}:
            continue
        ref_id = (movement.get("ref_id") or "").strip()
        if not ref_id:
            continue

        row = grouped.setdefault(ref_id, {
            "movimiento_id": ref_id,
            "fecha": (movement.get("fecha") or "").strip(),
            "tipo": "compra",
            "descripcion": "",
            "importe_valor": 0.0,
            "ref_id": ref_id,
            "usuario": "",
            "origen": "compras",
            "notes": [],
        })

        note = (movement.get("nota") or "").strip()
        row["notes"].append(note)
        if not row["usuario"]:
            row["usuario"] = _extract_note_value(note, "Usr:")

        qty = _to_float(movement.get("cantidad", "0"))
        unit_price = _to_float(_extract_note_value(note, "€/u:"))
        row["importe_valor"] += round(qty * unit_price, 2)

    rows: List[Dict[str, object]] = []
    for row in grouped.values():
        amount = round(float(row["importe_valor"]), 2)
        if abs(amount) < 1e-9:
            continue
        rows.append({
            "movimiento_id": row["movimiento_id"],
            "fecha": row["fecha"],
            "tipo": "compra",
            "descripcion": _collect_purchase_description(list(row["notes"])),
            "importe_valor": -amount,
            "ref_id": row["ref_id"],
            "usuario": row["usuario"],
            "origen": row["origen"],
        })
    return rows


def _expense_movements(expenses_csv: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for expense in read_all(expenses_csv):
        amount = round(_to_float(expense.get("importe", "0")), 2)
        if abs(amount) < 1e-9:
            continue
        ref_id = (expense.get("gasto_id") or "").strip()
        rows.append({
            "movimiento_id": ref_id or f"gasto-{len(rows) + 1}",
            "fecha": (expense.get("fecha") or "").strip(),
            "tipo": "gasto",
            "descripcion": (expense.get("descripcion") or "").strip(),
            "importe_valor": -amount,
            "ref_id": ref_id,
            "usuario": (expense.get("usuario") or "").strip(),
            "origen": "gastos",
        })
    return rows


def _adjustment_movements(logs_csv: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for log in read_all(logs_csv):
        if (log.get("accion") or "").strip().upper() != "AJUSTE CAJA":
            continue
        detail = (log.get("detalle") or "").strip()
        match = _ADJUSTMENT_RE.search(detail)
        if not match:
            continue
        previous_balance = round(_to_float(match.group(1)), 2)
        new_balance = round(_to_float(match.group(2)), 2)
        note = match.group(3).strip()
        delta = round(new_balance - previous_balance, 2)
        if abs(delta) < 1e-9:
            continue
        movement_id = (log.get("log_id") or "").strip() or f"ajuste-{len(rows) + 1}"
        rows.append({
            "movimiento_id": movement_id,
            "fecha": (log.get("fecha") or "").strip(),
            "tipo": "ajuste",
            "descripcion": note,
            "importe_valor": delta,
            "ref_id": "",
            "usuario": (log.get("usuario") or "").strip(),
            "origen": "ajustes",
        })
    return rows


def _migration_cash_config(logs_csv: Path) -> Dict[str, object] | None:
    latest: Dict[str, object] | None = None
    for log in read_all(logs_csv):
        if (log.get("accion") or "").strip().upper() != _MIGRATION_CASH_ACTION:
            continue

        detail = (log.get("detalle") or "").strip()
        cutoff_raw = _extract_note_value(detail, "corte=") or (log.get("fecha") or "").strip()
        cutoff_dt = _parse_datetime(cutoff_raw) or _parse_datetime(log.get("fecha", ""))
        if not cutoff_dt:
            continue

        payload = {
            "cutoff": cutoff_dt,
            "saldo_inicio_anio": round(_to_float(_extract_note_value(detail, "saldo_inicio_anio=")), 2),
            "saldo_actual": round(_to_float(_extract_note_value(detail, "saldo_actual=")), 2),
            "usuario": (log.get("usuario") or "").strip(),
        }
        if latest is None or cutoff_dt > latest["cutoff"]:
            latest = payload
    return latest


def _migration_anchor_movements(migration_cash: Dict[str, object]) -> List[Dict[str, object]]:
    cutoff_dt = migration_cash["cutoff"]
    year_start = datetime(cutoff_dt.year, 1, 1, 0, 0, 0)
    user = str(migration_cash.get("usuario") or "").strip()
    year_start_balance = round(float(migration_cash.get("saldo_inicio_anio") or 0.0), 2)
    current_balance = round(float(migration_cash.get("saldo_actual") or 0.0), 2)

    return [
        {
            "movimiento_id": f"ANCLA-{cutoff_dt.year}-INICIO",
            "fecha": year_start.strftime("%Y-%m-%d %H:%M:%S"),
            "tipo": "ancla",
            "descripcion": "Saldo inicial anual fijado en la migracion",
            "importe_valor": 0.0,
            "saldo_fijo": year_start_balance,
            "ref_id": "",
            "usuario": user,
            "origen": "migracion",
        },
        {
            "movimiento_id": f"ANCLA-{cutoff_dt.strftime('%Y%m%d%H%M%S')}-CORTE",
            "fecha": cutoff_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "tipo": "ancla",
            "descripcion": "Saldo real de caja al migrar",
            "importe_valor": 0.0,
            "saldo_fijo": current_balance,
            "ref_id": "",
            "usuario": user,
            "origen": "migracion",
        },
    ]


def _is_before_migration_cutoff(row: Dict[str, object], cutoff_day: date) -> bool:
    parsed = _parse_datetime(str(row.get("fecha") or ""))
    return bool(parsed and parsed.date() < cutoff_day)


def _daily_history_rows(movements: List[Dict[str, str]], opening_balance: float) -> List[Dict[str, str]]:
    today = date.today()
    updated_at = _now_iso()
    if not movements:
        return [{
            "fecha": today.isoformat(),
            "saldo_cierre": _format_money(opening_balance),
            "movimientos_dia": "0",
            "actualizado_en": updated_at,
        }]

    balances_by_day: dict[str, float] = {}
    counts_by_day: dict[str, int] = defaultdict(int)
    for movement in movements:
        day = (movement.get("fecha") or "")[:10]
        if not day:
            continue
        balances_by_day[day] = round(_to_float(movement.get("saldo", "0")), 2)
        counts_by_day[day] += 1

    first_day = _parse_datetime(movements[0].get("fecha", "")) or datetime.combine(today, datetime.min.time())
    last_day = _parse_datetime(movements[-1].get("fecha", "")) or datetime.combine(today, datetime.min.time())
    current_day = first_day.date()
    end_day = max(last_day.date(), today)

    rows: List[Dict[str, str]] = []
    running = round(opening_balance, 2)
    while current_day <= end_day:
        day_key = current_day.isoformat()
        if day_key in balances_by_day:
            running = balances_by_day[day_key]
        rows.append({
            "fecha": day_key,
            "saldo_cierre": _format_money(running),
            "movimientos_dia": str(counts_by_day.get(day_key, 0)),
            "actualizado_en": updated_at,
        })
        current_day += timedelta(days=1)
    return rows


def rebuild_cash_analysis_files(
    *,
    cashbox_csv: Path,
    invoices_csv: Path,
    inventory_movs_csv: Path,
    expenses_csv: Path,
    logs_csv: Path,
    cash_movements_csv: Path,
    cash_daily_csv: Path,
    backup_dir: Path | None = None,
) -> dict[str, object]:
    migration_cash = _migration_cash_config(logs_csv)
    cutoff_day = migration_cash["cutoff"].date() if migration_cash else None
    invoice_rows = _invoice_movements(invoices_csv)
    if migration_cash:
        invoice_rows = [
            row for row in invoice_rows
            if not bool(row.get("is_legacy")) and not _is_before_migration_cutoff(row, cutoff_day)
        ]
    purchase_rows = _purchase_movements(inventory_movs_csv)
    expense_rows = _expense_movements(expenses_csv)
    adjustment_rows = _adjustment_movements(logs_csv)
    if migration_cash:
        purchase_rows = [row for row in purchase_rows if not _is_before_migration_cutoff(row, cutoff_day)]
        expense_rows = [row for row in expense_rows if not _is_before_migration_cutoff(row, cutoff_day)]
        adjustment_rows = [row for row in adjustment_rows if not _is_before_migration_cutoff(row, cutoff_day)]

    raw_rows = (
        invoice_rows
        + purchase_rows
        + expense_rows
        + adjustment_rows
    )
    if migration_cash:
        raw_rows.extend(_migration_anchor_movements(migration_cash))
    raw_rows.sort(key=lambda row: _sort_key({
        "fecha": str(row.get("fecha") or ""),
        "tipo": str(row.get("tipo") or ""),
        "ref_id": str(row.get("ref_id") or ""),
        "movimiento_id": str(row.get("movimiento_id") or ""),
    }))

    current_balance = _current_cash_balance(cashbox_csv)
    anchor_rows = [row for row in raw_rows if row.get("saldo_fijo") is not None]
    if anchor_rows:
        opening_balance = round(float(anchor_rows[0].get("saldo_fijo") or 0.0), 2)
    else:
        total_delta = round(sum(float(row.get("importe_valor") or 0.0) for row in raw_rows), 2)
        opening_balance = round(current_balance - total_delta, 2)

    movements: List[Dict[str, str]] = []
    running_balance = opening_balance
    for row in raw_rows:
        if row.get("saldo_fijo") is not None:
            amount = 0.0
            running_balance = round(float(row.get("saldo_fijo") or 0.0), 2)
        else:
            amount = round(float(row.get("importe_valor") or 0.0), 2)
            running_balance = round(running_balance + amount, 2)
        movements.append({
            "movimiento_id": str(row.get("movimiento_id") or "").strip(),
            "fecha": str(row.get("fecha") or "").strip(),
            "tipo": str(row.get("tipo") or "").strip(),
            "descripcion": str(row.get("descripcion") or "").strip(),
            "importe": _format_money(amount),
            "saldo": _format_money(running_balance),
            "ref_id": str(row.get("ref_id") or "").strip(),
            "usuario": str(row.get("usuario") or "").strip(),
            "origen": str(row.get("origen") or "").strip(),
        })

    daily_rows = _daily_history_rows(movements, opening_balance)

    write_all_atomic(cash_movements_csv, movements, CASH_MOVEMENT_HEADERS, backup_dir=backup_dir)
    write_all_atomic(cash_daily_csv, daily_rows, CASH_DAILY_HEADERS, backup_dir=backup_dir)

    return {
        "opening_balance": opening_balance,
        "current_balance": current_balance,
        "generated_current_balance": round(_to_float(movements[-1].get("saldo", "0")) if movements else opening_balance, 2),
        "movements": len(movements),
        "days": len(daily_rows),
    }


def list_cash_detail_rows(
    cash_movements_csv: Path,
    *,
    start_date: date,
    end_date: date,
) -> List[Dict[str, str]]:
    rows = read_all(cash_movements_csv)
    rows.sort(key=_sort_key)

    result: List[Dict[str, str]] = []
    for row in rows:
        parsed = _parse_datetime(row.get("fecha", ""))
        if not parsed:
            continue
        row_date = parsed.date()
        if row_date < start_date or row_date > end_date:
            continue
        result.append({
            "fecha": row.get("fecha", "")[:10],
            "tipo": (row.get("tipo") or "").strip(),
            "descripcion": (row.get("descripcion") or "").strip(),
            "importe": _format_money(_to_float(row.get("importe", "0"))),
            "saldo": _format_money(_to_float(row.get("saldo", "0"))),
        })
    return result


def cash_detail_summary(
    cash_movements_csv: Path,
    *,
    start_date: date,
    end_date: date,
) -> Dict[str, float | int]:
    rows = read_all(cash_movements_csv)
    rows.sort(key=_sort_key)

    overall_opening = 0.0
    if rows:
        first_amount = _to_float(rows[0].get("importe", "0"))
        first_balance = _to_float(rows[0].get("saldo", "0"))
        overall_opening = round(first_balance - first_amount, 2)

    opening_balance = overall_opening
    filtered: List[Dict[str, str]] = []
    for row in rows:
        parsed = _parse_datetime(row.get("fecha", ""))
        if not parsed:
            continue
        row_date = parsed.date()
        if row_date < start_date:
            opening_balance = round(_to_float(row.get("saldo", "0")), 2)
            continue
        if row_date > end_date:
            break
        filtered.append(row)

    closing_balance = opening_balance
    if filtered:
        closing_balance = round(_to_float(filtered[-1].get("saldo", "0")), 2)

    income_total = round(
        sum(_to_float(row.get("importe", "0")) for row in filtered if _to_float(row.get("importe", "0")) > 0),
        2,
    )
    expense_total = round(
        sum(abs(_to_float(row.get("importe", "0"))) for row in filtered if _to_float(row.get("importe", "0")) < 0),
        2,
    )
    net_total = round(sum(_to_float(row.get("importe", "0")) for row in filtered), 2)

    return {
        "count": len(filtered),
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "income_total": income_total,
        "expense_total": expense_total,
        "net_total": net_total,
    }


def cash_annual_summary(
    cash_movements_csv: Path,
    *,
    start_date: date,
    end_date: date,
) -> Dict[str, object]:
    rows = read_all(cash_movements_csv)
    rows.sort(key=_sort_key)
    today = date.today()
    data_end_date = min(end_date, today) if start_date.year == today.year else end_date

    overall_opening = 0.0
    if rows:
        first_amount = _to_float(rows[0].get("importe", "0"))
        first_balance = _to_float(rows[0].get("saldo", "0"))
        overall_opening = round(first_balance - first_amount, 2)

    opening_balance = overall_opening
    grouped: dict[tuple[int, int], List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        parsed = _parse_datetime(row.get("fecha", ""))
        if not parsed:
            continue
        row_date = parsed.date()
        if row_date < start_date:
            opening_balance = round(_to_float(row.get("saldo", "0")), 2)
            continue
        if row_date > data_end_date:
            break
        grouped[(row_date.year, row_date.month)].append(row)

    month_rows: List[Dict[str, object]] = []
    current_month = date(start_date.year, start_date.month, 1)
    end_month = date(end_date.year, end_date.month, 1)
    running_balance = opening_balance
    closing_balance = opening_balance

    while current_month <= end_month:
        month_key = (current_month.year, current_month.month)
        current_rows = grouped.get(month_key, [])
        income_total = round(
            sum(_to_float(row.get("importe", "0")) for row in current_rows if _to_float(row.get("importe", "0")) > 0),
            2,
        )
        expense_total = round(
            sum(abs(_to_float(row.get("importe", "0"))) for row in current_rows if _to_float(row.get("importe", "0")) < 0),
            2,
        )
        if current_rows:
            running_balance = round(_to_float(current_rows[-1].get("saldo", "0")), 2)
            closing_balance = running_balance
        display_balance = running_balance if current_month <= date(data_end_date.year, data_end_date.month, 1) else 0.0

        month_rows.append({
            "year": current_month.year,
            "month": current_month.month,
            "income_total": income_total,
            "expense_total": expense_total,
            "saldo": display_balance,
        })

        if current_month.month == 12:
            current_month = date(current_month.year + 1, 1, 1)
        else:
            current_month = date(current_month.year, current_month.month + 1, 1)

    return {
        "rows": month_rows,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "net_balance": round(closing_balance - opening_balance, 2),
    }


def list_cash_monthly_balances(
    cash_daily_csv: Path,
    *,
    start_date: date,
    end_date: date,
) -> List[Dict[str, object]]:
    latest_by_month: dict[tuple[int, int], Dict[str, object]] = {}

    for row in read_all(cash_daily_csv):
        parsed = _parse_datetime(row.get("fecha", ""))
        if not parsed:
            continue

        row_date = parsed.date()
        if row_date < start_date or row_date > end_date:
            continue

        key = (row_date.year, row_date.month)
        current = latest_by_month.get(key)
        payload = {
            "fecha": row_date,
            "saldo": round(_to_float(row.get("saldo_cierre", "0")), 2),
        }
        if current is None or row_date > current["fecha"]:
            latest_by_month[key] = payload

    ordered_keys = sorted(latest_by_month)
    return [
        {
            "fecha": latest_by_month[key]["fecha"],
            "saldo": latest_by_month[key]["saldo"],
        }
        for key in ordered_keys
    ]
