from __future__ import annotations
from typing import Optional, Dict, List
from uuid import uuid4

from werkzeug.security import generate_password_hash, check_password_hash

from .csv_store import read_all, append_rows
from pathlib import Path

USERS_HEADERS = ["user_id", "username", "password_hash", "activo"]

def ensure_default_admin(users_csv: Path) -> None:
    users = read_all(users_csv)
    if any(u.get("username") == "admin" for u in users):
        return

    row = {
        "user_id": str(uuid4()),
        "username": "admin",
        "password_hash": generate_password_hash("admin123"),
        "activo": "1",
    }
    append_rows(users_csv, [row], USERS_HEADERS)

def find_user_by_username(users_csv: Path, username: str) -> Optional[Dict[str, str]]:
    username = (username or "").strip()
    if not username:
        return None
    for u in read_all(users_csv):
        if u.get("username", "").strip().lower() == username.lower():
            return u
    return None

def verify_login(users_csv: Path, username: str, password: str) -> bool:
    u = find_user_by_username(users_csv, username)
    if not u:
        return False
    if u.get("activo", "1") != "1":
        return False
    return check_password_hash(u.get("password_hash", ""), password or "")