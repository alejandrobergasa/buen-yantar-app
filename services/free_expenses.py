from __future__ import annotations

from datetime import datetime
from heapq import nlargest
from pathlib import Path
from typing import Dict, List

from .csv_store import append_rows, read_all, write_all_atomic
from .ids import prefixed_id

FREE_EXPENSE_HEADERS = [
    "gasto_id",
    "fecha",
    "categoria",
    "descripcion",
    "importe",
    "usuario",
]

FREE_EXPENSE_CATEGORIES = [
    "Limpieza",
    "Extra",
    "Traspaso Cuenta",
]

FREE_EXPENSE_CATEGORY_EMOJIS = {
    "Limpieza": "🧼",
    "Extra": "📦",
    "Traspaso Cuenta": "🏦",
}


def free_expense_category_label(category: str) -> str:
    clean_category = (category or "").strip()
    if not clean_category:
        return "Sin categoria"
    emoji = FREE_EXPENSE_CATEGORY_EMOJIS.get(clean_category)
    if not emoji:
        return clean_category
    return f"{emoji} {clean_category}"


def _expense_ts(expense_date: str) -> str:
    raw = (expense_date or "").strip()
    if not raw:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        base_dt = datetime.strptime(raw, "%Y-%m-%d")
        return base_dt.strftime("%Y-%m-%d 00:00:00")
    except ValueError:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_free_expense(
    expenses_csv: Path,
    *,
    amount: float,
    description: str,
    category: str,
    expense_date: str = "",
    user: str = "",
) -> tuple[str, float]:
    expense_id = prefixed_id("GL")
    amount_value = round(float(amount), 2)
    row = {
        "gasto_id": expense_id,
        "fecha": _expense_ts(expense_date),
        "categoria": (category or "").strip(),
        "descripcion": (description or "").strip(),
        "importe": f"{amount_value:.2f}",
        "usuario": (user or "").strip(),
    }
    append_rows(expenses_csv, [row], FREE_EXPENSE_HEADERS)
    return expense_id, amount_value


def list_free_expenses(expenses_csv: Path, limit: int = 400) -> List[Dict[str, str]]:
    rows = read_all(expenses_csv)
    if limit <= 0:
        rows.sort(key=lambda x: x.get("fecha", ""), reverse=True)
        return rows
    return nlargest(limit, rows, key=lambda x: x.get("fecha", ""))


def delete_free_expense(
    expenses_csv: Path,
    expense_id: str,
    *,
    backup_dir: Path | None = None,
) -> Dict[str, str] | None:
    target = (expense_id or "").strip()
    if not target:
        return None

    rows = read_all(expenses_csv)
    expense = next(
        (row for row in rows if (row.get("gasto_id") or "").strip() == target),
        None,
    )
    if expense is None:
        return None

    remaining_rows = [
        row for row in rows
        if (row.get("gasto_id") or "").strip() != target
    ]
    write_all_atomic(expenses_csv, remaining_rows, FREE_EXPENSE_HEADERS, backup_dir=backup_dir)
    return expense
