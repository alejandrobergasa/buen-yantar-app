from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List
from uuid import uuid4

from .csv_store import append_rows, read_all

AUDIT_HEADERS = [
    "log_id",
    "fecha",
    "usuario",
    "accion",
    "detalle",
]


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_action(
    logs_csv: Path,
    usuario: str,
    accion: str,
    detalle: str = "",
) -> None:
    row = {
        "log_id": str(uuid4()),
        "fecha": now_iso(),
        "usuario": (usuario or "").strip() or "anon",
        "accion": (accion or "").strip(),
        "detalle": (detalle or "").strip(),
    }
    append_rows(logs_csv, [row], AUDIT_HEADERS)


def list_logs(logs_csv: Path, limit: int = 1200) -> List[Dict[str, str]]:
    rows = read_all(logs_csv)
    rows.sort(key=lambda x: x.get("fecha", ""), reverse=True)
    if limit <= 0:
        return rows
    return rows[:limit]
