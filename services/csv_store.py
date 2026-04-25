from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Iterable, Optional

_WRITE_LOCK = Lock()
_CACHE_LOCK = Lock()
_READ_CACHE: dict[str, tuple[int, int, List[Dict[str, str]]]] = {}


def _cache_key(path: Path) -> str:
    return str(path.resolve())


def _clone_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [dict(row) for row in rows]


def _invalidate_cache(path: Path) -> None:
    with _CACHE_LOCK:
        _READ_CACHE.pop(_cache_key(path), None)

def ensure_csv(path: Path, headers: List[str]) -> None:
    """
    Crea el CSV con cabecera si no existe o si existe pero está vacío.
    Esto evita el problema típico de "archivo vacío sin headers".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if (not path.exists()) or path.stat().st_size == 0:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()

def read_all(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        _invalidate_cache(path)
        return []

    stat = path.stat()
    key = _cache_key(path)
    with _CACHE_LOCK:
        cached = _READ_CACHE.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return _clone_rows(cached[2])

    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)

    with _CACHE_LOCK:
        _READ_CACHE[key] = (stat.st_mtime_ns, stat.st_size, _clone_rows(rows))
    return rows

def append_rows(path: Path, rows: Iterable[Dict[str, str]], headers: List[str]) -> None:
    with _WRITE_LOCK:
        ensure_csv(path, headers)
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            for row in rows:
                w.writerow(row)
        _invalidate_cache(path)

def write_all_atomic(path: Path, rows: List[Dict[str, str]], headers: List[str], backup_dir: Optional[Path] = None) -> None:
    with _WRITE_LOCK:
        ensure_csv(path, headers)

        if backup_dir is not None and path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(path, backup_dir / f"{path.stem}_{ts}.bak.csv")

        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            for row in rows:
                w.writerow(row)

        tmp.replace(path)
        _invalidate_cache(path)
