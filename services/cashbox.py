from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict

from .csv_store import ensure_csv, read_all, write_all_atomic

CASHBOX_HEADERS = [
    "saldo_actual",
    "actualizado_en",
    "actualizado_por",
    "nota",
]


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


def ensure_cashbox(cashbox_csv: Path) -> None:
    ensure_csv(cashbox_csv, CASHBOX_HEADERS)
    rows = read_all(cashbox_csv)
    if rows:
        return

    write_all_atomic(
        cashbox_csv,
        [{
            "saldo_actual": "0.00",
            "actualizado_en": "",
            "actualizado_por": "",
            "nota": "",
        }],
        CASHBOX_HEADERS,
    )


def get_cashbox_state(cashbox_csv: Path) -> Dict[str, object]:
    ensure_cashbox(cashbox_csv)
    rows = read_all(cashbox_csv)
    row = rows[0] if rows else {}
    balance = round(_to_float(row.get("saldo_actual", "0")), 2)
    return {
        "saldo_actual": f"{balance:.2f}",
        "saldo_actual_valor": balance,
        "actualizado_en": (row.get("actualizado_en") or "").strip(),
        "actualizado_por": (row.get("actualizado_por") or "").strip(),
        "nota": (row.get("nota") or "").strip(),
    }


def get_cash_balance(cashbox_csv: Path) -> float:
    return float(get_cashbox_state(cashbox_csv)["saldo_actual_valor"])


def set_cash_balance(
    cashbox_csv: Path,
    amount: float,
    updated_by: str = "",
    note: str = "",
) -> Dict[str, object]:
    ensure_cashbox(cashbox_csv)
    balance = round(float(amount), 2)
    row = {
        "saldo_actual": f"{balance:.2f}",
        "actualizado_en": _now_iso(),
        "actualizado_por": (updated_by or "").strip(),
        "nota": (note or "").strip(),
    }
    write_all_atomic(cashbox_csv, [row], CASHBOX_HEADERS)
    return get_cashbox_state(cashbox_csv)


def adjust_cash_balance(
    cashbox_csv: Path,
    delta: float,
    updated_by: str = "",
    note: str = "",
) -> Dict[str, object]:
    current = get_cash_balance(cashbox_csv)
    return set_cash_balance(
        cashbox_csv,
        amount=current + float(delta),
        updated_by=updated_by,
        note=note,
    )
