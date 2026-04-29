from __future__ import annotations

import re
from typing import Dict, List, Optional

from markupsafe import Markup, escape


EMOJI_ASSETS: Dict[str, Dict[str, object]] = {
    "package": {"char": "📦", "label": "Paquete", "file": "package.svg", "aliases": ["\uFE0F📦"], "category": "object"},
    "receipt": {"char": "🧾", "label": "Factura", "file": "receipt.svg", "aliases": [], "category": "system"},
    "cart": {"char": "🛒", "label": "Carrito", "file": "cart.svg", "aliases": [], "category": "object"},
    "cash": {"char": "💸", "label": "Dinero", "file": "cash.svg", "aliases": [], "category": "system"},
    "document": {"char": "📄", "label": "Documento", "file": "document.svg", "aliases": [], "category": "object"},
    "bags": {"char": "🛍️", "label": "Compras", "file": "bags.svg", "aliases": ["🛍"], "category": "object"},
    "card": {"char": "💳", "label": "Tarjeta", "file": "card.svg", "aliases": [], "category": "system"},
    "user": {"char": "👤", "label": "Usuario", "file": "user.svg", "aliases": [], "category": "system"},
    "settings": {"char": "⚙️", "label": "Configuración", "file": "settings.svg", "aliases": ["⚙"], "category": "system"},
    "chart": {"char": "📈", "label": "Análisis", "file": "chart.svg", "aliases": [], "category": "system"},
    "infinity": {"char": "♾️", "label": "Infinito", "file": "infinity.svg", "aliases": ["♾"], "category": "system"},
    "warning": {"char": "🟡", "label": "Aviso", "file": "warning.svg", "aliases": [], "category": "system"},
    "check": {"char": "✅", "label": "Correcto", "file": "check.svg", "aliases": [], "category": "system"},
    "trash": {"char": "🗑️", "label": "Eliminar", "file": "trash.svg", "aliases": ["🗑"], "category": "system"},
    "save": {"char": "💾", "label": "Guardar", "file": "save.svg", "aliases": [], "category": "system"},
    "beer": {"char": "🍺", "label": "Cerveza", "file": "beer.svg", "aliases": [], "category": "drink"},
    "wine": {"char": "🍷", "label": "Vino", "file": "wine.svg", "aliases": [], "category": "drink"},
    "cocktail": {"char": "🍹", "label": "Coctel", "file": "cocktail.svg", "aliases": [], "category": "drink"},
    "coffee": {"char": "☕", "label": "Cafe", "file": "coffee.svg", "aliases": [], "category": "drink"},
    "milk": {"char": "🥛", "label": "Lacteos", "file": "milk.svg", "aliases": [], "category": "drink"},
    "bottle": {"char": "🍾", "label": "Botellas", "file": "bottle.svg", "aliases": [], "category": "drink"},
    "apple": {"char": "🍎", "label": "Fruta", "file": "apple.svg", "aliases": [], "category": "food"},
    "banana": {"char": "🍌", "label": "Platano", "file": "banana.svg", "aliases": [], "category": "food"},
    "bread": {"char": "🍞", "label": "Panaderia", "file": "bread.svg", "aliases": [], "category": "food"},
    "carrot": {"char": "🥕", "label": "Verduras", "file": "carrot.svg", "aliases": [], "category": "food"},
    "leafy": {"char": "🥬", "label": "Hortalizas", "file": "leafy.svg", "aliases": [], "category": "food"},
    "cheese": {"char": "🧀", "label": "Quesos", "file": "cheese.svg", "aliases": [], "category": "food"},
    "egg": {"char": "🥚", "label": "Huevos", "file": "egg.svg", "aliases": [], "category": "food"},
    "fish": {"char": "🐟", "label": "Pescado", "file": "fish.svg", "aliases": [], "category": "food"},
    "meat": {"char": "🥩", "label": "Carne", "file": "meat.svg", "aliases": [], "category": "food"},
    "pan": {"char": "🍳", "label": "Cocina", "file": "pan.svg", "aliases": [], "category": "food"},
    "can": {"char": "🥫", "label": "Conservas", "file": "can.svg", "aliases": [], "category": "food"},
    "ice": {"char": "🧊", "label": "Congelados", "file": "ice.svg", "aliases": [], "category": "food"},
    "basket": {"char": "🧺", "label": "Cestas", "file": "basket.svg", "aliases": [], "category": "object"},
    "box": {"char": "🗃️", "label": "Cajas", "file": "box.svg", "aliases": ["🗃"], "category": "object"},
    "jar": {"char": "🫙", "label": "Tarros", "file": "jar.svg", "aliases": [], "category": "object"},
    "cleaning": {"char": "🧼", "label": "Limpieza", "file": "cleaning.svg", "aliases": [], "category": "object"},
    "tag": {"char": "🏷️", "label": "Etiquetas", "file": "tag.svg", "aliases": ["🏷"], "category": "object"},
    "bank": {"char": "🏦", "label": "Banco", "file": "bank.svg", "aliases": [], "category": "system"},
    "puzzle": {"char": "🧩", "label": "Varios", "file": "puzzle.svg", "aliases": [], "category": "object"},
}

DEFAULT_EMOJI_KEY = "package"
EMOJI_PICKER_KEYS: List[str] = [
    "apple",
    "banana",
    "bread",
    "carrot",
    "leafy",
    "cheese",
    "egg",
    "fish",
    "meat",
    "can",
    "pan",
    "ice",
    "beer",
    "wine",
    "cocktail",
    "coffee",
    "milk",
    "bottle",
    "package",
    "basket",
    "box",
    "jar",
    "cleaning",
    "tag",
    "puzzle",
]

_VALUE_TO_KEY: Dict[str, str] = {}
for _key, _entry in EMOJI_ASSETS.items():
    _VALUE_TO_KEY[_key] = _key
    _VALUE_TO_KEY[str(_entry["char"])] = _key
    _VALUE_TO_KEY[str(_entry["char"]).replace("\uFE0F", "")] = _key
    for _alias in _entry.get("aliases", []):
        _VALUE_TO_KEY[str(_alias)] = _key
        _VALUE_TO_KEY[str(_alias).replace("\uFE0F", "")] = _key

_EMOJI_VALUES = sorted(
    {value for value in _VALUE_TO_KEY if value and value != DEFAULT_EMOJI_KEY},
    key=len,
    reverse=True,
)
_EMOJI_PATTERN = re.compile("|".join(re.escape(value) for value in _EMOJI_VALUES))


def get_emoji_key(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_EMOJI_KEY
    normalized = raw.replace("\uFE0F", "")
    return _VALUE_TO_KEY.get(raw) or _VALUE_TO_KEY.get(normalized) or DEFAULT_EMOJI_KEY


def get_emoji_entry(value: object) -> Dict[str, object]:
    return EMOJI_ASSETS[get_emoji_key(value)]


def emoji_asset_records(url_builder) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for key, entry in EMOJI_ASSETS.items():
        records.append({
            "key": key,
            "char": entry["char"],
            "label": entry["label"],
            "alt": entry["label"],
            "aliases": list(entry.get("aliases", [])),
            "category": entry.get("category") or "object",
            "file": entry["file"],
            "url": url_builder(str(entry["file"])),
        })
    return records


def render_emoji_html(value: object, *, url_builder, alt: Optional[str] = None, class_name: str = "") -> Markup:
    entry = get_emoji_entry(value)
    classes = "emoji-icon"
    if class_name:
        classes = f"{classes} {class_name}"
    src = escape(url_builder(str(entry["file"])))
    label = escape(alt or str(entry["label"]))
    return Markup(
        f'<img class="{escape(classes)}" src="{src}" alt="{label}" loading="lazy" decoding="async">'
    )


def replace_emoji_text(value: object, *, url_builder, class_name: str = "emoji-inline") -> Markup:
    text = str(value or "")
    if not text:
        return Markup("")

    parts: List[str] = []
    last_index = 0
    for match in _EMOJI_PATTERN.finditer(text):
        if match.start() > last_index:
            parts.append(str(escape(text[last_index:match.start()])))
        parts.append(str(render_emoji_html(match.group(0), url_builder=url_builder, class_name=class_name)))
        last_index = match.end()
    if last_index < len(text):
        parts.append(str(escape(text[last_index:])))
    return Markup("".join(parts))
