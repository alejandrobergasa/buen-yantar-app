from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import random
import math

import csv

from .csv_store import read_all, append_rows, write_all_atomic
from .ids import prefixed_id, short_id

# Campos de producto:
# - unidad: unidad de compra / stock
# - unidad_venta: unidad usada al facturar
# - fracciones_por_unidad: cuantas unidades de venta caben en una unidad de stock
PRODUCT_HEADERS = [
    "producto_id", "nombre", "precio_unitario", "unidad",
    "stock_minimo", "grupo", "grupo_emoji",
    "stock_infinito",
    "fraccionable", "fracciones_por_unidad", "unidad_venta",
    "activo"
]

MOV_HEADERS = ["mov_id", "fecha", "producto_id", "tipo", "cantidad", "origen", "ref_id", "nota"]

TRUTHY_VALUES = {"1", "true", "True", "on", "yes", "si", "sí"}
_PRODUCTS_CACHE: Dict[tuple[str, int, int], Dict[str, object]] = {}
_STOCK_CACHE: Dict[tuple[str, int, int], Dict[str, float]] = {}
_PURCHASES_CACHE: Dict[tuple[str, int, int], Dict[str, List[Dict[str, str]]]] = {}


def _file_cache_key(path: Path) -> tuple[str, int, int]:
    resolved = str(path.resolve())
    if not path.exists():
        return (resolved, 0, 0)
    stat = path.stat()
    return (resolved, stat.st_mtime_ns, stat.st_size)


def _format_number(value: float, decimals: int = 4) -> str:
    if abs(value - int(value)) < 1e-9:
        return str(int(value))
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def _parse_fraction_count(raw: object) -> float:
    try:
        value = float(str(raw or "").strip().replace(",", "."))
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0


def _normalize_product_row(row: Dict[str, str]) -> Dict[str, str]:
    stock_unit = (row.get("unidad") or "ud").strip() or "ud"
    fraction_count = _parse_fraction_count(row.get("fracciones_por_unidad"))
    fractional = (row.get("fraccionable") or "").strip() in TRUTHY_VALUES and fraction_count > 0
    sale_unit_raw = (row.get("unidad_venta") or "").strip()
    sale_unit = sale_unit_raw or stock_unit

    normalized = dict(row)
    normalized.update({
        "precio_unitario": (row.get("precio_unitario") or "").strip(),
        "unidad": stock_unit,
        "stock_minimo": (row.get("stock_minimo") or "0").strip() or "0",
        "grupo": (row.get("grupo") or "Otros").strip() or "Otros",
        "grupo_emoji": (row.get("grupo_emoji") or "📦").strip() or "📦",
        "stock_infinito": "1" if (row.get("stock_infinito") or "").strip() in TRUTHY_VALUES else "0",
        "fraccionable": "1" if fractional else "0",
        "fracciones_por_unidad": _format_number(fraction_count),
        "unidad_venta": sale_unit if fractional else stock_unit,
        "activo": "0" if (row.get("activo") or "").strip() == "0" else "1",
    })
    return normalized


def product_is_fractionable(product: Dict[str, str]) -> bool:
    return (product.get("fraccionable") or "").strip() in TRUTHY_VALUES and _parse_fraction_count(product.get("fracciones_por_unidad")) > 0


def product_fraction_count(product: Dict[str, str]) -> float:
    return _parse_fraction_count(product.get("fracciones_por_unidad"))


def product_sale_unit(product: Dict[str, str]) -> str:
    stock_unit = (product.get("unidad") or "ud").strip() or "ud"
    sale_unit = (product.get("unidad_venta") or "").strip() or stock_unit
    return sale_unit if product_is_fractionable(product) else stock_unit


def sale_quantity_to_stock_quantity(product: Dict[str, str], sale_quantity: float) -> float:
    if not product_is_fractionable(product):
        return sale_quantity
    return sale_quantity / product_fraction_count(product)


def stock_quantity_to_sale_quantity(product: Dict[str, str], stock_quantity: float) -> float:
    if not product_is_fractionable(product):
        return stock_quantity
    return stock_quantity * product_fraction_count(product)


def purchase_price_breakdown(product: Dict[str, str], quantity: float, entered_price: float) -> Dict[str, float]:
    qty = max(float(quantity or 0), 0.0)
    price = max(float(entered_price or 0), 0.0)

    if product_is_fractionable(product):
        line_total = round(price, 2)
        unit_purchase_price = (price / qty) if qty > 0 else 0.0
        sale_unit_purchase_price = unit_purchase_price / product_fraction_count(product)
    else:
        line_total = round(qty * price, 2)
        unit_purchase_price = price
        sale_unit_purchase_price = price

    return {
        "line_total": line_total,
        "unit_purchase_price": unit_purchase_price,
        "sale_unit_purchase_price": sale_unit_purchase_price,
        "recommended_sale_price": round(sale_unit_purchase_price * 1.2, 2),
    }


def ensure_product_schema(productos_csv: Path, backup_dir: Optional[Path] = None) -> None:
    """
    Migra productos.csv al esquema esperado.
    Soporta:
    - CSV legado sin columna stock_infinito.
    - CSV previos a productos fraccionables.
    - Filas desalineadas por escrituras previas con cabecera antigua.
    """
    if not productos_csv.exists() or productos_csv.stat().st_size == 0:
        return

    with productos_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    if headers == PRODUCT_HEADERS:
        return

    migrated: List[Dict[str, str]] = []
    for r in rows:
        raw_activo = (r.get("activo") or "").strip()
        raw_stock_inf = (r.get("stock_infinito") or "").strip()
        extra = r.get(None) or []
        extra0 = (extra[0] or "").strip() if extra else ""

        # Caso típico de cabecera antigua:
        # - activo contiene realmente stock_infinito
        # - primera extra contiene el activo real
        if not raw_stock_inf and raw_activo in {"0", "1"} and extra0 in {"0", "1"}:
            stock_infinito = raw_activo
            activo = extra0
        else:
            stock_infinito = raw_stock_inf if raw_stock_inf in {"0", "1"} else "0"
            activo = raw_activo if raw_activo in {"0", "1"} else "1"

        migrated.append(_normalize_product_row({
            "producto_id": (r.get("producto_id") or "").strip(),
            "nombre": (r.get("nombre") or "").strip(),
            "precio_unitario": (r.get("precio_unitario") or "").strip(),
            "unidad": (r.get("unidad") or "ud").strip() or "ud",
            "stock_minimo": (r.get("stock_minimo") or "0").strip() or "0",
            "grupo": (r.get("grupo") or "Otros").strip() or "Otros",
            "grupo_emoji": (r.get("grupo_emoji") or "📦").strip() or "📦",
            "stock_infinito": stock_infinito,
            "fraccionable": (r.get("fraccionable") or "").strip(),
            "fracciones_por_unidad": (r.get("fracciones_por_unidad") or "").strip() or "1",
            "unidad_venta": (r.get("unidad_venta") or "").strip(),
            "activo": activo,
        }))

    write_all_atomic(productos_csv, migrated, PRODUCT_HEADERS, backup_dir=backup_dir)

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_demo_products(productos_csv: Path) -> None:
    """
    Si el CSV está vacío, crea MUCHOS productos de prueba.
    """
    prods = read_all(productos_csv)
    if prods:
        return

    grupos = [
        ("Bebidas", "🍺"),
        ("Cocina", "🍳"),
        ("Despensa", "🥫"),
        ("Limpieza", "🧼"),
        ("Carne", "🥩"),
        ("Congelados", "🧊"),
        ("Otros", "📦"),
    ]

    nombres_base = [
        "Cerveza", "Vino tinto", "Vino blanco", "Agua", "Refresco cola", "Tónica",
        "Aceite de oliva", "Sal", "Azúcar", "Harina", "Arroz", "Pasta", "Tomate frito",
        "Atún", "Mayonesa", "Ketchup", "Mostaza", "Vinagre", "Café", "Té",
        "Detergente", "Lavavajillas", "Bayetas", "Papel cocina", "Papel higiénico",
        "Filetes", "Chorizo", "Jamón", "Pollo", "Hamburguesas",
        "Hielo", "Patatas congeladas", "Verdura congelada", "Helado",
        "Servilletas", "Vasos", "Platos", "Pan", "Queso", "Leche",
    ]

    demo: List[Dict[str, str]] = []
    for i in range(1, 121):
        base = random.choice(nombres_base)
        suf = f" {i:03d}"
        grupo, emoji = random.choice(grupos)

        unidad = "ud"
        if base in ["Harina", "Arroz", "Azúcar"]:
            unidad = "kg"
        if base == "Agua":
            unidad = "l"

        precio = round(random.uniform(0.6, 12.0), 2)
        stock_min = random.choice([0, 2, 4, 6, 12, 24])

        # un pequeño porcentaje con stock infinito para probar
        infinito = "1" if random.random() < 0.08 else "0"

        demo.append({
            "producto_id": short_id(),
            "nombre": (base + suf),
            "precio_unitario": f"{precio:.2f}",
            "unidad": unidad,
            "stock_minimo": str(stock_min),
            "grupo": grupo,
            "grupo_emoji": emoji,
            "stock_infinito": infinito,
            "fraccionable": "0",
            "fracciones_por_unidad": "1",
            "unidad_venta": unidad,
            "activo": "1",
        })

    append_rows(productos_csv, demo, PRODUCT_HEADERS)

def get_products(productos_csv: Path) -> List[Dict[str, str]]:
    cache_key = _file_cache_key(productos_csv)
    cached = _PRODUCTS_CACHE.get(cache_key)
    if cached is None:
        products: List[Dict[str, str]] = []
        by_id: Dict[str, Dict[str, str]] = {}
        for row in read_all(productos_csv):
            normalized = _normalize_product_row(row)
            if normalized.get("activo", "1") != "1":
                continue
            products.append(normalized)
            product_id = normalized.get("producto_id", "")
            if product_id:
                by_id[product_id] = normalized
        cached = {
            "products": products,
            "by_id": by_id,
        }
        _PRODUCTS_CACHE.clear()
        _PRODUCTS_CACHE[cache_key] = cached
    return list(cached["products"])

def find_product(productos_csv: Path, producto_id: str) -> Optional[Dict[str, str]]:
    cache_key = _file_cache_key(productos_csv)
    cached = _PRODUCTS_CACHE.get(cache_key)
    if cached is None:
        get_products(productos_csv)
        cached = _PRODUCTS_CACHE.get(cache_key)
    if cached is None:
        return None
    product = cached["by_id"].get(producto_id)
    return dict(product) if product else None

def create_product(
    productos_csv: Path,
    nombre: str,
    precio_unitario: str,
    unidad: str,
    stock_minimo: str,
    grupo: str,
    grupo_emoji: str,
    stock_infinito: str,
    fraccionable: str = "0",
    fracciones_por_unidad: str = "1",
    unidad_venta: str = "",
) -> str:
    pid = short_id()
    row = _normalize_product_row({
        "producto_id": pid,
        "nombre": nombre.strip(),
        "precio_unitario": (precio_unitario or "").strip(),
        "unidad": (unidad or "ud").strip(),
        "stock_minimo": (stock_minimo or "0").strip(),
        "grupo": (grupo or "Otros").strip(),
        "grupo_emoji": (grupo_emoji or "📦").strip(),
        "stock_infinito": "1" if (stock_infinito or "").strip() in TRUTHY_VALUES else "0",
        "fraccionable": fraccionable,
        "fracciones_por_unidad": fracciones_por_unidad,
        "unidad_venta": unidad_venta,
        "activo": "1",
    })
    append_rows(productos_csv, [row], PRODUCT_HEADERS)
    return pid

def update_product_fields(productos_csv: Path, backup_dir: Path, producto_id: str, fields: Dict[str, str]) -> None:
    update_many_product_fields(
        productos_csv,
        backup_dir=backup_dir,
        updates={producto_id: fields},
    )


def update_many_product_fields(
    productos_csv: Path,
    backup_dir: Path,
    updates: Dict[str, Dict[str, str]],
) -> None:
    if not updates:
        return

    rows = read_all(productos_csv)
    changed = False
    for r in rows:
        product_id = (r.get("producto_id") or "").strip()
        fields = updates.get(product_id)
        if fields:
            row_changed = False
            for k, v in fields.items():
                if k in r and r.get(k) != v:
                    r[k] = v
                    row_changed = True
            if row_changed:
                normalized = _normalize_product_row(r)
                r.update({key: normalized.get(key, "") for key in PRODUCT_HEADERS})
                changed = True
    if changed:
        write_all_atomic(productos_csv, rows, PRODUCT_HEADERS, backup_dir=backup_dir)

def calc_stock_by_product(movs_csv: Path) -> Dict[str, float]:
    """
    Calcula stock desde movimientos.
    NOTA: para stock infinito lo gestionamos fuera (en la vista), no aquí.
    """
    cache_key = _file_cache_key(movs_csv)
    cached = _STOCK_CACHE.get(cache_key)
    if cached is None:
        stock: Dict[str, float] = {}
        for m in read_all(movs_csv):
            pid = m.get("producto_id", "")
            tipo = (m.get("tipo") or "").strip().upper()
            try:
                qty = float(m.get("cantidad") or 0)
            except ValueError:
                qty = 0.0

            if tipo == "ENTRADA":
                stock[pid] = stock.get(pid, 0.0) + qty
            elif tipo == "SALIDA":
                stock[pid] = stock.get(pid, 0.0) - qty
            elif tipo == "AJUSTE":
                stock[pid] = stock.get(pid, 0.0) + qty
        _STOCK_CACHE.clear()
        _STOCK_CACHE[cache_key] = stock
        cached = stock
    return cached

def last_purchases_for_product(movs_csv: Path, producto_id: str, limit: int = 200) -> List[Dict[str, str]]:
    """
    Devuelve ENTRADAS del producto, ordenadas de más reciente a más antigua.
    """
    cache_key = _file_cache_key(movs_csv)
    cached = _PURCHASES_CACHE.get(cache_key)
    if cached is None:
        by_product: Dict[str, List[Dict[str, str]]] = {}
        for m in read_all(movs_csv):
            pid = m.get("producto_id", "")
            tipo = (m.get("tipo") or "").strip().upper()
            origen = (m.get("origen") or "").strip().upper()
            if origen != "COMPRA" or tipo not in {"ENTRADA", "INFO"}:
                continue
            by_product.setdefault(pid, []).append(m)
        for rows in by_product.values():
            rows.sort(key=lambda x: x.get("fecha", ""), reverse=True)
        _PURCHASES_CACHE.clear()
        _PURCHASES_CACHE[cache_key] = by_product
        cached = by_product
    rows = cached.get(producto_id, [])
    return rows[:limit] if limit > 0 else list(rows)

def add_adjustment(movs_csv: Path, producto_id: str, delta: float, nota: str = "Ajuste manual") -> None:
    row = {
        "mov_id": short_id(),
        "fecha": now_iso(),
        "producto_id": producto_id,
        "tipo": "AJUSTE",
        "cantidad": str(delta),
        "origen": "AJUSTE",
        "ref_id": "",
        "nota": nota,
    }
    append_rows(movs_csv, [row], MOV_HEADERS)

def set_stock_to_value(movs_csv: Path, producto_id: str, desired_stock: float) -> None:
    stock = calc_stock_by_product(movs_csv).get(producto_id, 0.0)
    delta = desired_stock - stock
    if abs(delta) < 1e-9:
        return
    add_adjustment(movs_csv, producto_id, delta, nota=f"Ajuste stock a {desired_stock}")


def add_purchase_entries(
    movs_csv: Path,
    lines: List[Dict[str, str]],
    provider: str = "",
    global_note: str = "",
    purchase_date: str = "",
    user: str = "",
) -> str:
    """
    Registra una compra con 1..N líneas como movimientos ENTRADA y devuelve ref_id.
    Cada elemento de lines debe incluir: producto_id, cantidad y opcionalmente nota/precio_compra.
    """
    ref_id = prefixed_id("CP")

    provider = (provider or "").strip()
    global_note = (global_note or "").strip()
    purchase_date = (purchase_date or "").strip()
    user = (user or "").strip()

    # Fecha del ticket (YYYY-MM-DD). Si no viene o es inválida, usamos ahora.
    if purchase_date:
        try:
            base_dt = datetime.strptime(purchase_date, "%Y-%m-%d")
            move_ts = base_dt.strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            move_ts = now_iso()
    else:
        move_ts = now_iso()
    rows: List[Dict[str, str]] = []

    for line in lines:
        producto_id = (line.get("producto_id") or "").strip()
        cantidad = str(line.get("cantidad") or "").strip()
        precio_compra = str(line.get("precio_compra") or "").strip()
        nota_linea = (line.get("nota") or "").strip()
        stock_infinito = (line.get("stock_infinito") or "").strip() == "1"

        note_parts: List[str] = []
        if user:
            note_parts.append(f"Usr: {user}")
        if provider:
            note_parts.append(f"Prov: {provider}")
        if precio_compra:
            note_parts.append(f"€/u: {precio_compra}")
        if global_note:
            note_parts.append(global_note)
        if nota_linea:
            note_parts.append(nota_linea)

        rows.append({
            "mov_id": short_id(),
            "fecha": move_ts,
            "producto_id": producto_id,
            "tipo": "INFO" if stock_infinito else "ENTRADA",
            "cantidad": cantidad,
            "origen": "COMPRA",
            "ref_id": ref_id,
            "nota": " | ".join(note_parts),
        })

    append_rows(movs_csv, rows, MOV_HEADERS)
    return ref_id
