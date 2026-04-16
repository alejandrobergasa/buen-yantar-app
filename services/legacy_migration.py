from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from werkzeug.security import generate_password_hash

from .auth import USERS_HEADERS
from .audit import AUDIT_HEADERS
from .cashbox import CASHBOX_HEADERS
from .csv_store import ensure_csv, write_all_atomic
from .free_expenses import FREE_EXPENSE_HEADERS
from .groups import GROUP_HEADERS
from .ids import prefixed_id, short_id
from .inventory import MOV_HEADERS, PRODUCT_HEADERS
from .invoices import INVOICE_HEADERS, INVOICE_LINE_HEADERS


USERS_FILENAME = "buenyantarusuarios.txt"
INVENTORY_FILENAME = "buenyantarinventario.txt"
INVOICES_FILENAME = "buenyantarfacturas.txt"
LEGACY_INVOICE_PREFIX = "LG"


@dataclass
class LegacyMigrationResult:
    users: int
    products: int
    groups: int
    invoices: int
    invoice_lines: int
    stock_adjustments: int
    placeholders: int


def _read_legacy_lines(path: Path) -> List[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _parse_decimal(raw: object) -> float:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        return 0.0
    return float(text.replace(",", "."))


def _format_amount(value: float) -> str:
    return f"{value:.2f}"


def _format_qty(value: float) -> str:
    if abs(value - int(value)) < 1e-9:
        return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _group_name_from_legacy(raw: object) -> str:
    text = str(raw or "").strip()
    return f"Grupo {text}" if text else "Otros"


def _invoice_iso_date(raw: str) -> str:
    parsed = datetime.strptime((raw or "").strip(), "%d/%m/%Y")
    return parsed.strftime("%Y-%m-%d 00:00:00")


def _ensure_legacy_files(folder: Path) -> tuple[Path, Path, Path]:
    users_path = folder / USERS_FILENAME
    inventory_path = folder / INVENTORY_FILENAME
    invoices_path = folder / INVOICES_FILENAME
    missing = [path.name for path in (users_path, inventory_path, invoices_path) if not path.exists()]
    if missing:
        raise ValueError(f"Faltan archivos legacy: {', '.join(missing)}")
    return users_path, inventory_path, invoices_path


def migrate_legacy_dataset(
    *,
    legacy_folder: Path,
    saldo_final_caja: float,
    saldo_inicio_anio: float,
    csv_usuarios: Path,
    csv_productos: Path,
    csv_grupos: Path,
    csv_movs: Path,
    csv_facturas: Path,
    csv_factura_lineas: Path,
    csv_caja: Path,
    csv_logs: Path,
    csv_gastos: Path,
    backup_dir: Path,
    imported_by: str = "",
) -> LegacyMigrationResult:
    users_path, inventory_path, invoices_path = _ensure_legacy_files(legacy_folder)
    migration_dt = datetime.now().replace(microsecond=0)
    migration_ts = migration_dt.strftime("%Y-%m-%d %H:%M:%S")
    migration_user = (imported_by or "").strip() or "system"

    legacy_inventory = _read_legacy_lines(inventory_path)
    product_rows: List[Dict[str, str]] = []
    groups_map: Dict[str, Dict[str, str]] = {}
    product_by_name: Dict[str, Dict[str, str]] = {}
    desired_stock: Dict[str, float] = {}

    for line in legacy_inventory:
        parts = line.split("|")
        if len(parts) != 5:
            continue
        name, stock_raw, price_raw, stock_min_raw, legacy_group = parts
        product_id = short_id()
        stock_value = _parse_decimal(stock_raw)
        group_name = _group_name_from_legacy(legacy_group)

        product_row = {
            "producto_id": product_id,
            "nombre": name.strip(),
            "precio_unitario": _format_amount(_parse_decimal(price_raw)),
            "unidad": "ud",
            "stock_minimo": _format_qty(_parse_decimal(stock_min_raw)),
            "grupo": group_name,
            "grupo_emoji": "📦",
            "stock_infinito": "0",
            "fraccionable": "0",
            "fracciones_por_unidad": "1",
            "unidad_venta": "ud",
            "activo": "1",
        }
        product_rows.append(product_row)
        product_by_name[name.strip().lower()] = product_row
        desired_stock[product_id] = stock_value
        groups_map.setdefault(group_name.lower(), {
            "group_id": short_id(),
            "nombre": group_name,
            "emoji": "📦",
            "activo": "1",
        })

    legacy_users = _read_legacy_lines(users_path)
    user_rows: List[Dict[str, str]] = []
    for line in legacy_users:
        parts = line.split("|")
        if len(parts) != 4:
            continue
        username, password_hash, legacy_role, _display_name = parts
        user_rows.append({
            "user_id": short_id(),
            "username": username.strip(),
            "password_hash": (password_hash or "").strip() or generate_password_hash(username.strip()),
            "rol": "admin" if (legacy_role or "").strip() == "0" else "normal",
            "activo": "1",
        })

    if not any((row.get("username") or "").strip().lower() == "admin" for row in user_rows):
        user_rows.append({
            "user_id": short_id(),
            "username": "admin",
            "password_hash": generate_password_hash("admin123"),
            "rol": "admin",
            "activo": "1",
        })

    legacy_invoices = _read_legacy_lines(invoices_path)
    invoice_rows: List[Dict[str, str]] = []
    invoice_line_rows: List[Dict[str, str]] = []
    movement_rows: List[Dict[str, str]] = []
    sold_by_product: Dict[str, float] = defaultdict(float)
    placeholder_count = 0

    def ensure_placeholder_product(product_name: str, unit_price: float) -> Dict[str, str]:
        nonlocal placeholder_count
        key = product_name.strip().lower()
        existing = product_by_name.get(key)
        if existing:
            return existing
        placeholder_count += 1
        group_name = "Migrados sin grupo"
        groups_map.setdefault(group_name.lower(), {
            "group_id": short_id(),
            "nombre": group_name,
            "emoji": "🧩",
            "activo": "1",
        })
        row = {
            "producto_id": short_id(),
            "nombre": product_name.strip(),
            "precio_unitario": _format_amount(unit_price),
            "unidad": "ud",
            "stock_minimo": "0",
            "grupo": group_name,
            "grupo_emoji": "🧩",
            "stock_infinito": "0",
            "fraccionable": "0",
            "fracciones_por_unidad": "1",
            "unidad_venta": "ud",
            "activo": "1",
        }
        product_rows.append(row)
        product_by_name[key] = row
        desired_stock[row["producto_id"]] = 0.0
        return row

    for line in legacy_invoices:
        parts = line.split("|")
        if len(parts) < 4:
            continue
        username, _display_name, legacy_date, declared_lines_raw = parts[:4]
        item_parts = parts[4:]
        declared_lines = int((declared_lines_raw or "0").strip() or "0")

        if len(item_parts) % 3 != 0:
            raise ValueError(f"Factura legacy con estructura inválida en fecha {legacy_date}: {line}")

        factura_id = prefixed_id(LEGACY_INVOICE_PREFIX)
        invoice_ts = _invoice_iso_date(legacy_date)
        total_amount = 0.0
        actual_lines = 0

        for index in range(0, len(item_parts), 3):
            product_name = item_parts[index].strip()
            unit_price = _parse_decimal(item_parts[index + 1])
            qty = _parse_decimal(item_parts[index + 2])
            if not product_name:
                continue

            product = ensure_placeholder_product(product_name, unit_price)
            importe = round(unit_price * qty, 2)
            total_amount += importe
            actual_lines += 1
            sold_by_product[product["producto_id"]] += qty

            invoice_line_rows.append({
                "linea_id": short_id(),
                "factura_id": factura_id,
                "producto_id": product["producto_id"],
                "producto_nombre": product["nombre"],
                "unidad": "ud",
                "cantidad": _format_qty(qty),
                "precio_unitario": _format_amount(unit_price),
                "importe_linea": _format_amount(importe),
                "nota": "",
            })
            movement_rows.append({
                "mov_id": short_id(),
                "fecha": invoice_ts,
                "producto_id": product["producto_id"],
                "tipo": "SALIDA",
                "cantidad": _format_qty(qty),
                "origen": "FACTURA",
                "ref_id": factura_id,
                "nota": "",
            })

        invoice_rows.append({
            "factura_id": factura_id,
            "fecha": invoice_ts,
            "cliente": "",
            "nota_global": "",
            "total_importe": _format_amount(total_amount),
            "lineas": str(actual_lines),
            "usuario": username.strip(),
        })

    stock_adjustments = 0
    for product in product_rows:
        if product.get("stock_infinito") == "1":
            continue
        product_id = product["producto_id"]
        needed_initial_stock = desired_stock.get(product_id, 0.0) + sold_by_product.get(product_id, 0.0)
        if abs(needed_initial_stock) < 1e-9:
            continue
        stock_adjustments += 1
        movement_rows.append({
            "mov_id": short_id(),
            "fecha": "1970-01-01 00:00:00",
            "producto_id": product_id,
            "tipo": "AJUSTE",
            "cantidad": _format_qty(needed_initial_stock),
            "origen": "AJUSTE",
            "ref_id": "",
            "nota": "Migracion legacy · stock inicial reconstruido",
        })

    group_rows = list(groups_map.values())
    group_rows.sort(key=lambda row: row["nombre"].lower())

    cash_row = {
        "saldo_actual": _format_amount(round(float(saldo_final_caja), 2)),
        "actualizado_en": migration_ts,
        "actualizado_por": migration_user,
        "nota": (
            f"Migracion legacy desde {legacy_folder} | "
            f"saldo_inicio_anio={_format_amount(round(float(saldo_inicio_anio), 2))}"
        ),
    }
    log_rows = [{
        "log_id": short_id(),
        "fecha": migration_ts,
        "usuario": migration_user,
        "accion": "MIGRACION CAJA",
        "detalle": (
            f"corte={migration_ts} | "
            f"saldo_inicio_anio={_format_amount(round(float(saldo_inicio_anio), 2))} | "
            f"saldo_actual={_format_amount(round(float(saldo_final_caja), 2))} | "
            f"carpeta={legacy_folder}"
        ),
    }]

    ensure_csv(csv_usuarios, USERS_HEADERS)
    ensure_csv(csv_productos, PRODUCT_HEADERS)
    ensure_csv(csv_grupos, GROUP_HEADERS)
    ensure_csv(csv_movs, MOV_HEADERS)
    ensure_csv(csv_facturas, INVOICE_HEADERS)
    ensure_csv(csv_factura_lineas, INVOICE_LINE_HEADERS)
    ensure_csv(csv_caja, CASHBOX_HEADERS)

    write_all_atomic(csv_usuarios, user_rows, USERS_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_productos, product_rows, PRODUCT_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_grupos, group_rows, GROUP_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_movs, movement_rows, MOV_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_facturas, invoice_rows, INVOICE_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_factura_lineas, invoice_line_rows, INVOICE_LINE_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_caja, [cash_row], CASHBOX_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_logs, log_rows, AUDIT_HEADERS, backup_dir=backup_dir)
    write_all_atomic(csv_gastos, [], FREE_EXPENSE_HEADERS, backup_dir=backup_dir)

    return LegacyMigrationResult(
        users=len(user_rows),
        products=len(product_rows),
        groups=len(group_rows),
        invoices=len(invoice_rows),
        invoice_lines=len(invoice_line_rows),
        stock_adjustments=stock_adjustments,
        placeholders=placeholder_count,
    )
