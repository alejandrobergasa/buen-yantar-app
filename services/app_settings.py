from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict

from .csv_store import ensure_csv, read_all, write_all_atomic

APP_SETTINGS_HEADERS = [
    "clave",
    "valor",
    "actualizado_en",
    "actualizado_por",
]

DEFAULT_APP_SETTINGS: Dict[str, str] = {
    "app_zoom_percent": "100",
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_app_settings(settings_csv: Path, backup_dir: Path | None = None) -> None:
    ensure_csv(settings_csv, APP_SETTINGS_HEADERS)
    rows = read_all(settings_csv)
    current_keys = {(row.get("clave") or "").strip() for row in rows}
    missing_rows = [
        {
            "clave": key,
            "valor": value,
            "actualizado_en": "",
            "actualizado_por": "",
        }
        for key, value in DEFAULT_APP_SETTINGS.items()
        if key not in current_keys
    ]
    if missing_rows:
        write_all_atomic(settings_csv, rows + missing_rows, APP_SETTINGS_HEADERS, backup_dir=backup_dir)


def get_app_settings(settings_csv: Path) -> Dict[str, str]:
    ensure_app_settings(settings_csv)
    settings = dict(DEFAULT_APP_SETTINGS)
    for row in read_all(settings_csv):
        key = (row.get("clave") or "").strip()
        if not key:
            continue
        settings[key] = (row.get("valor") or "").strip()
    return settings


def get_app_zoom_percent(settings_csv: Path) -> int:
    raw_value = get_app_settings(settings_csv).get("app_zoom_percent", "100")
    try:
        value = int(str(raw_value).strip())
    except ValueError:
        value = 100
    return max(50, min(200, value))


def set_app_setting(
    settings_csv: Path,
    *,
    key: str,
    value: str,
    updated_by: str = "",
    backup_dir: Path | None = None,
) -> None:
    ensure_app_settings(settings_csv, backup_dir=backup_dir)
    rows = read_all(settings_csv)
    clean_key = (key or "").strip()
    changed = False
    for row in rows:
        if (row.get("clave") or "").strip() != clean_key:
            continue
        row["valor"] = (value or "").strip()
        row["actualizado_en"] = _now_iso()
        row["actualizado_por"] = (updated_by or "").strip()
        changed = True
        break
    if not changed:
        rows.append({
            "clave": clean_key,
            "valor": (value or "").strip(),
            "actualizado_en": _now_iso(),
            "actualizado_por": (updated_by or "").strip(),
        })
    write_all_atomic(settings_csv, rows, APP_SETTINGS_HEADERS, backup_dir=backup_dir)
