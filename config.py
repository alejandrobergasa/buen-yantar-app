import os
import platform
import struct
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backups"

CSV_USUARIOS = DATA_DIR / "usuarios.csv"
CSV_PRODUCTOS = DATA_DIR / "productos.csv"
CSV_GRUPOS = DATA_DIR / "grupos_productos.csv"
CSV_MOVS = DATA_DIR / "movimientos_inventario.csv"
CSV_FACTURAS = DATA_DIR / "facturas.csv"
CSV_FACTURA_LINEAS = DATA_DIR / "facturas_lineas.csv"
CSV_LOGS = DATA_DIR / "logs_acciones.csv"
CSV_CAJA = DATA_DIR / "caja.csv"
CSV_CAJA_MOVIMIENTOS = DATA_DIR / "caja_movimientos.csv"
CSV_CAJA_HISTORIAL = DATA_DIR / "caja_historial_diario.csv"
CSV_GASTOS_LIBRES = DATA_DIR / "gastos_libres.csv"
CSV_APP_SETTINGS = DATA_DIR / "app_settings.csv"
PRINT_JOBS_DIR = DATA_DIR / "print_jobs"

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-cambia-esto")
AUDIT_READ_REQUESTS = os.getenv("AUDIT_READ_REQUESTS", "0").strip().lower() in {"1", "true", "yes", "on"}


def _auto_low_resource_mode() -> bool:
    if platform.system() != "Windows":
        return False

    release = platform.release().strip()
    is_legacy_windows = release == "7"
    is_32bit_python = struct.calcsize("P") == 4
    return is_legacy_windows or is_32bit_python


def _read_low_resource_mode() -> bool:
    raw = os.getenv("LOW_RESOURCE_MODE", "auto").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return _auto_low_resource_mode()


LOW_RESOURCE_MODE = _read_low_resource_mode()

def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    PRINT_JOBS_DIR.mkdir(parents=True, exist_ok=True)
