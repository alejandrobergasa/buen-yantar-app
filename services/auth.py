from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from .csv_store import append_rows, read_all, write_all_atomic
from .ids import short_id

USERS_HEADERS = ["user_id", "username", "password_hash", "rol", "activo"]


def ensure_user_schema(users_csv: Path, backup_dir: Optional[Path] = None) -> None:
    if not users_csv.exists() or users_csv.stat().st_size == 0:
        return

    with users_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    if headers == USERS_HEADERS:
        return

    migrated: List[Dict[str, str]] = []
    for r in rows:
        username = (r.get("username") or "").strip()
        role = (r.get("rol") or "").strip().lower()
        if role not in {"admin", "normal"}:
            role = "admin" if username.lower() == "admin" else "normal"

        migrated.append({
            "user_id": (r.get("user_id") or short_id()).strip(),
            "username": username,
            "password_hash": (r.get("password_hash") or "").strip(),
            "rol": role,
            "activo": (r.get("activo") or "1").strip() or "1",
        })

    write_all_atomic(users_csv, migrated, USERS_HEADERS, backup_dir=backup_dir)


def ensure_default_admin(users_csv: Path) -> None:
    users = read_all(users_csv)
    if any((u.get("username") or "").strip().lower() == "admin" for u in users):
        return

    row = {
        "user_id": short_id(),
        "username": "admin",
        "password_hash": generate_password_hash("admin123"),
        "rol": "admin",
        "activo": "1",
    }
    append_rows(users_csv, [row], USERS_HEADERS)


def list_users(users_csv: Path) -> List[Dict[str, str]]:
    rows = read_all(users_csv)
    rows.sort(key=lambda x: (x.get("username") or "").lower())
    return rows


def find_user_by_username(users_csv: Path, username: str) -> Optional[Dict[str, str]]:
    username = (username or "").strip()
    if not username:
        return None
    for u in read_all(users_csv):
        if (u.get("username", "").strip().lower()) == username.lower():
            return u
    return None


def verify_login(users_csv: Path, username: str, password: str) -> bool:
    u = find_user_by_username(users_csv, username)
    if not u:
        return False
    if u.get("activo", "1") != "1":
        return False
    return check_password_hash(u.get("password_hash", ""), password or "")


def is_admin(user_row: Optional[Dict[str, str]]) -> bool:
    if not user_row:
        return False
    return (user_row.get("rol") or "normal").strip().lower() == "admin"


def create_user(users_csv: Path, username: str, password: str, rol: str = "normal") -> None:
    username = (username or "").strip()
    if not username:
        raise ValueError("Username vacío")
    if find_user_by_username(users_csv, username):
        raise ValueError("El usuario ya existe")

    role_clean = (rol or "normal").strip().lower()
    if role_clean not in {"admin", "normal"}:
        role_clean = "normal"

    row = {
        "user_id": short_id(),
        "username": username,
        "password_hash": generate_password_hash(password or ""),
        "rol": role_clean,
        "activo": "1",
    }
    append_rows(users_csv, [row], USERS_HEADERS)


def update_password(users_csv: Path, backup_dir: Path, username: str, new_password: str) -> None:
    rows = read_all(users_csv)
    changed = False
    for r in rows:
        if (r.get("username") or "").strip().lower() == (username or "").strip().lower():
            r["password_hash"] = generate_password_hash(new_password or "")
            changed = True
            break
    if changed:
        write_all_atomic(users_csv, rows, USERS_HEADERS, backup_dir=backup_dir)


def delete_user(users_csv: Path, backup_dir: Path, username: str) -> None:
    rows = read_all(users_csv)
    kept = [
        r for r in rows
        if (r.get("username") or "").strip().lower() != (username or "").strip().lower()
    ]
    if len(kept) != len(rows):
        # Mantener al menos un admin activo.
        active_admins = [
            u for u in kept
            if (u.get("activo", "1") == "1")
            and (u.get("rol") or "normal").strip().lower() == "admin"
        ]
        if not active_admins:
            raise ValueError("Debe quedar al menos un administrador activo.")

        write_all_atomic(users_csv, kept, USERS_HEADERS, backup_dir=backup_dir)
    else:
        # no-op si no existe
        return
