import csv
import shutil
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Iterable, Optional

_WRITE_LOCK = Lock()

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
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r)

def append_rows(path: Path, rows: Iterable[Dict[str, str]], headers: List[str]) -> None:
    with _WRITE_LOCK:
        ensure_csv(path, headers)
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            for row in rows:
                w.writerow(row)

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
