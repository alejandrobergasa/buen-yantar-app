from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from .csv_store import ensure_csv, read_all, write_all_atomic
from .ids import short_id


GROUP_HEADERS = ["group_id", "nombre", "emoji", "activo"]


def _normalize_group_name(raw: object) -> str:
    return str(raw or "").strip() or "Otros"


def _normalize_group_emoji(raw: object) -> str:
    return str(raw or "").strip() or "📦"


def ensure_group_catalog(
    groups_csv: Path,
    products: List[Dict[str, str]] | None = None,
    *,
    products_csv: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> None:
    ensure_csv(groups_csv, GROUP_HEADERS)

    rows = read_all(groups_csv)
    existing_by_name = {
        _normalize_group_name(row.get("nombre")).lower(): row
        for row in rows
        if (row.get("activo") or "1").strip() != "0"
    }

    source_products = products if products is not None else (read_all(products_csv) if products_csv is not None else [])
    changed = False
    for product in source_products:
        group_name = _normalize_group_name(product.get("grupo"))
        group_key = group_name.lower()
        if group_key in existing_by_name:
            continue
        group_row = {
            "group_id": short_id(),
            "nombre": group_name,
            "emoji": _normalize_group_emoji(product.get("grupo_emoji")),
            "activo": "1",
        }
        rows.append(group_row)
        existing_by_name[group_key] = group_row
        changed = True

    if not rows:
        rows.append({
            "group_id": short_id(),
            "nombre": "Otros",
            "emoji": "📦",
            "activo": "1",
        })
        changed = True

    if changed:
        write_all_atomic(groups_csv, rows, GROUP_HEADERS, backup_dir=backup_dir)


def list_groups(groups_csv: Path) -> List[Dict[str, str]]:
    rows = [
        row for row in read_all(groups_csv)
        if (row.get("activo") or "1").strip() != "0"
    ]
    rows.sort(key=lambda row: _normalize_group_name(row.get("nombre")).lower())
    return rows


def find_group_by_name(groups_csv: Path, group_name: str) -> Optional[Dict[str, str]]:
    target = _normalize_group_name(group_name).lower()
    for row in list_groups(groups_csv):
        if _normalize_group_name(row.get("nombre")).lower() == target:
            return row
    return None


def create_group(groups_csv: Path, *, group_name: str, emoji: str, backup_dir: Optional[Path] = None) -> Dict[str, str]:
    name = _normalize_group_name(group_name)
    emoji_clean = _normalize_group_emoji(emoji)
    rows = read_all(groups_csv)

    for row in rows:
        if _normalize_group_name(row.get("nombre")).lower() != name.lower():
            continue
        row["emoji"] = emoji_clean
        row["activo"] = "1"
        write_all_atomic(groups_csv, rows, GROUP_HEADERS, backup_dir=backup_dir)
        return row

    new_row = {
        "group_id": short_id(),
        "nombre": name,
        "emoji": emoji_clean,
        "activo": "1",
    }
    rows.append(new_row)
    write_all_atomic(groups_csv, rows, GROUP_HEADERS, backup_dir=backup_dir)
    return new_row


def delete_group(groups_csv: Path, *, group_name: str, backup_dir: Optional[Path] = None) -> bool:
    target = _normalize_group_name(group_name).lower()
    rows = read_all(groups_csv)
    changed = False
    for row in rows:
        if _normalize_group_name(row.get("nombre")).lower() != target:
            continue
        if (row.get("activo") or "1").strip() == "0":
            return False
        row["activo"] = "0"
        changed = True
        break
    if changed:
        write_all_atomic(groups_csv, rows, GROUP_HEADERS, backup_dir=backup_dir)
    return changed
