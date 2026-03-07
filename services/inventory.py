from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4
import random
import math

import csv

from .csv_store import read_all, append_rows, write_all_atomic

# NUEVO: stock_infinito ("1" => infinito, "0" => normal)
PRODUCT_HEADERS = [
    "producto_id", "nombre", "precio_unitario", "unidad",
    "stock_minimo", "grupo", "grupo_emoji",
    "stock_infinito",
    "activo"
]

MOV_HEADERS = ["mov_id", "fecha", "producto_id", "tipo", "cantidad", "origen", "ref_id", "nota"]


def ensure_product_schema(productos_csv: Path, backup_dir: Optional[Path] = None) -> None:
    """
    Migra productos.csv al esquema esperado (incluye stock_infinito).
    Soporta:
    - CSV legado sin columna stock_infinito.
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

        migrated.append({
            "producto_id": (r.get("producto_id") or "").strip(),
            "nombre": (r.get("nombre") or "").strip(),
            "precio_unitario": (r.get("precio_unitario") or "").strip(),
            "unidad": (r.get("unidad") or "ud").strip() or "ud",
            "stock_minimo": (r.get("stock_minimo") or "0").strip() or "0",
            "grupo": (r.get("grupo") or "Otros").strip() or "Otros",
            "grupo_emoji": (r.get("grupo_emoji") or "📦").strip() or "📦",
            "stock_infinito": stock_infinito,
            "activo": activo,
        })

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
            "producto_id": str(uuid4()),
            "nombre": (base + suf),
            "precio_unitario": f"{precio:.2f}",
            "unidad": unidad,
            "stock_minimo": str(stock_min),
            "grupo": grupo,
            "grupo_emoji": emoji,
            "stock_infinito": infinito,
            "activo": "1",
        })

    append_rows(productos_csv, demo, PRODUCT_HEADERS)

def get_products(productos_csv: Path) -> List[Dict[str, str]]:
    return [p for p in read_all(productos_csv) if p.get("activo", "1") == "1"]

def find_product(productos_csv: Path, producto_id: str) -> Optional[Dict[str, str]]:
    for p in read_all(productos_csv):
        if p.get("producto_id") == producto_id and p.get("activo", "1") == "1":
            return p
    return None

def create_product(
    productos_csv: Path,
    nombre: str,
    precio_unitario: str,
    unidad: str,
    stock_minimo: str,
    grupo: str,
    grupo_emoji: str,
    stock_infinito: str,
) -> str:
    pid = str(uuid4())
    row = {
        "producto_id": pid,
        "nombre": nombre.strip(),
        "precio_unitario": (precio_unitario or "").strip(),
        "unidad": (unidad or "ud").strip(),
        "stock_minimo": (stock_minimo or "0").strip(),
        "grupo": (grupo or "Otros").strip(),
        "grupo_emoji": (grupo_emoji or "📦").strip(),
        "stock_infinito": "1" if (stock_infinito or "").strip() in ["1", "true", "True", "on"] else "0",
        "activo": "1",
    }
    append_rows(productos_csv, [row], PRODUCT_HEADERS)
    return pid

def update_product_fields(productos_csv: Path, backup_dir: Path, producto_id: str, fields: Dict[str, str]) -> None:
    rows = read_all(productos_csv)
    changed = False
    for r in rows:
        if r.get("producto_id") == producto_id:
            for k, v in fields.items():
                if k in r:
                    r[k] = v
                    changed = True
            break
    if changed:
        write_all_atomic(productos_csv, rows, PRODUCT_HEADERS, backup_dir=backup_dir)

def calc_stock_by_product(movs_csv: Path) -> Dict[str, float]:
    """
    Calcula stock desde movimientos.
    NOTA: para stock infinito lo gestionamos fuera (en la vista), no aquí.
    """
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
    return stock

def last_purchases_for_product(movs_csv: Path, producto_id: str, limit: int = 200) -> List[Dict[str, str]]:
    """
    Devuelve ENTRADAS del producto, ordenadas de más reciente a más antigua.
    """
    rows = []
    for m in read_all(movs_csv):
        if m.get("producto_id") != producto_id:
            continue
        if (m.get("tipo") or "").strip().upper() != "ENTRADA":
            continue
        rows.append(m)
    rows.sort(key=lambda x: x.get("fecha", ""), reverse=True)
    return rows[:limit]

def add_adjustment(movs_csv: Path, producto_id: str, delta: float, nota: str = "Ajuste manual") -> None:
    row = {
        "mov_id": str(uuid4()),
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
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    ref_id = f"CP-{ts}-{str(uuid4())[:8]}"

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
            "mov_id": str(uuid4()),
            "fecha": move_ts,
            "producto_id": producto_id,
            "tipo": "ENTRADA",
            "cantidad": cantidad,
            "origen": "COMPRA",
            "ref_id": ref_id,
            "nota": " | ".join(note_parts),
        })

    append_rows(movs_csv, rows, MOV_HEADERS)
    return ref_id
