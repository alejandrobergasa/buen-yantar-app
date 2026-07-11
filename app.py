from __future__ import annotations

from flask import Flask, render_template, request, redirect, url_for, session, flash, g, Response, jsonify
from datetime import date, datetime, timedelta
import csv
import io
import json
import math
import os
import sys
import unicodedata
from pathlib import Path
import config

from services.csv_store import ensure_csv, read_all
from services.audit import AUDIT_HEADERS, log_action, list_logs
from services.cashbox import (
    CASHBOX_HEADERS,
    adjust_cash_balance,
    ensure_cashbox,
    get_cash_balance,
    get_cashbox_state,
    set_cash_balance,
)
from services.cash_analysis import (
    rebuild_cash_analysis_files,
    list_cash_detail_rows,
    cash_detail_summary,
    cash_annual_summary,
)
from services.free_expenses import (
    FREE_EXPENSE_CATEGORIES,
    FREE_EXPENSE_HEADERS,
    create_free_expense,
    delete_free_expense,
    free_expense_category_label,
    list_free_expenses,
)
from services.groups import (
    GROUP_HEADERS,
    create_group,
    delete_group,
    ensure_group_catalog,
    find_group_by_id,
    find_group_by_name,
    list_groups,
    update_group,
)
from services.auth import (
    ensure_default_admin,
    verify_login,
    USERS_HEADERS,
    ensure_user_schema,
    find_user_by_username,
    is_admin,
    list_users,
    create_user,
    update_password,
    delete_user,
)
from services.app_settings import ensure_app_settings, get_app_zoom_percent, set_app_setting
from services.purchases import delete_purchase, list_purchase_history
from services.legacy_migration import migrate_legacy_dataset
from services.inventory import (
    PRODUCT_HEADERS, MOV_HEADERS,
    ensure_product_schema,
    ensure_demo_products,
    get_products, find_product,
    calc_stock_by_product, last_purchases_for_product,
    update_product_fields, update_many_product_fields, set_stock_to_value,
    create_product, add_purchase_entries,
    product_is_fractionable, product_fraction_count, product_sale_unit,
    sale_quantity_to_stock_quantity, stock_quantity_to_sale_quantity,
    purchase_price_breakdown,
)
from services.invoices import (
    INVOICE_HEADERS, INVOICE_LINE_HEADERS,
    create_invoice, list_invoices, list_invoice_lines,
    delete_invoice,
    list_invoice_lines_for_ids,
    find_invoice, list_invoice_lines_for,
)
from services.receipt_printer import format_invoice_ticket, format_shopping_list_ticket, print_text_ticket
from services.excel_export import build_cash_annual_workbook, build_cash_detail_workbook
from services.pdf_export import build_markdown_pdf
from services.emoji_assets import EMOJI_PICKER_KEYS, emoji_asset_records, get_emoji_entry, get_emoji_key, render_emoji_html, replace_emoji_text

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    # --- init storage ---
    config.ensure_dirs()
    ensure_csv(config.CSV_USUARIOS, USERS_HEADERS)
    ensure_csv(config.CSV_PRODUCTOS, PRODUCT_HEADERS)
    ensure_csv(config.CSV_GRUPOS, GROUP_HEADERS)
    ensure_csv(config.CSV_MOVS, MOV_HEADERS)
    ensure_csv(config.CSV_FACTURAS, INVOICE_HEADERS)
    ensure_csv(config.CSV_FACTURA_LINEAS, INVOICE_LINE_HEADERS)
    ensure_csv(config.CSV_LOGS, AUDIT_HEADERS)
    ensure_csv(config.CSV_CAJA, CASHBOX_HEADERS)
    ensure_csv(config.CSV_GASTOS_LIBRES, FREE_EXPENSE_HEADERS)
    ensure_app_settings(config.CSV_APP_SETTINGS, backup_dir=config.BACKUP_DIR)
    ensure_user_schema(config.CSV_USUARIOS, backup_dir=config.BACKUP_DIR)
    ensure_product_schema(config.CSV_PRODUCTOS, backup_dir=config.BACKUP_DIR)
    ensure_cashbox(config.CSV_CAJA)

    ensure_default_admin(config.CSV_USUARIOS)
    if os.getenv("LOAD_DEMO_DATA", "0") == "1":
        ensure_demo_products(config.CSV_PRODUCTOS)

    def sync_group_catalog_safe(
        *,
        products: list[dict[str, str]] | None = None,
        products_csv: Path | None = None,
    ) -> None:
        try:
            ensure_group_catalog(
                config.CSV_GRUPOS,
                products=products,
                products_csv=products_csv,
                backup_dir=config.BACKUP_DIR,
            )
        except Exception as exc:
            print(
                f"[BuenYantar] Aviso: no se pudo sincronizar el catalogo de grupos: {exc}",
                file=sys.stderr,
            )

    sync_group_catalog_safe(products_csv=config.CSV_PRODUCTOS)

    def emoji_url(filename: str) -> str:
        return url_for("static", filename=f"assets/emojis/{filename}")

    @app.context_processor
    def inject_emoji_helpers():
        assets = emoji_asset_records(emoji_url)
        assets_by_key = {str(asset["key"]): asset for asset in assets}
        picker_assets = [
            assets_by_key[key]
            for key in EMOJI_PICKER_KEYS
            if key in assets_by_key
        ]
        palette_css = Path(app.static_folder or "") / "home_menu_palette.css"
        palette_css_mtime = int(palette_css.stat().st_mtime) if palette_css.exists() else 0
        return {
            "emoji_icon": lambda value, alt=None, class_name="": render_emoji_html(
                value,
                url_builder=emoji_url,
                alt=alt,
                class_name=class_name,
            ),
            "emoji_text": lambda value, class_name="emoji-inline": replace_emoji_text(
                value,
                url_builder=emoji_url,
                class_name=class_name,
            ),
            "emoji_entry": get_emoji_entry,
            "emoji_picker_assets": picker_assets,
            "emoji_assets_json": json.dumps(assets, ensure_ascii=False),
            "palette_css_mtime": palette_css_mtime,
        }

    def refresh_cash_analysis_storage(force: bool = False) -> dict[str, object]:
        source_paths = (
            config.CSV_CAJA,
            config.CSV_FACTURAS,
            config.CSV_MOVS,
            config.CSV_GASTOS_LIBRES,
            config.CSV_LOGS,
        )
        output_paths = (
            config.CSV_CAJA_MOVIMIENTOS,
            config.CSV_CAJA_HISTORIAL,
        )

        if not force and all(path.exists() for path in output_paths):
            latest_source_mtime = max(path.stat().st_mtime_ns for path in source_paths if path.exists())
            earliest_output_mtime = min(path.stat().st_mtime_ns for path in output_paths)
            if earliest_output_mtime >= latest_source_mtime:
                return {
                    "cached": True,
                }

        return rebuild_cash_analysis_files(
            cashbox_csv=config.CSV_CAJA,
            invoices_csv=config.CSV_FACTURAS,
            inventory_movs_csv=config.CSV_MOVS,
            expenses_csv=config.CSV_GASTOS_LIBRES,
            logs_csv=config.CSV_LOGS,
            cash_movements_csv=config.CSV_CAJA_MOVIMIENTOS,
            cash_daily_csv=config.CSV_CAJA_HISTORIAL,
        )

    def require_login():
        if not session.get("user"):
            return redirect(url_for("login"))
        return None

    def current_user_row() -> dict | None:
        username = session.get("user", "")
        if not username:
            return None
        return find_user_by_username(config.CSV_USUARIOS, username)

    def user_is_admin() -> bool:
        return is_admin(current_user_row())

    def require_admin():
        r = require_login()
        if r:
            return r
        if not user_is_admin():
            flash("Acceso solo para administradores.")
            return redirect(url_for("home"))
        return None

    def require_admin_json(message: str = "Acceso solo para administradores."):
        r = require_login()
        if r:
            return jsonify({"ok": False, "error": "Sesion no valida."}), 401
        if not user_is_admin():
            return jsonify({"ok": False, "error": message}), 403
        return None

    def require_login_json(message: str = "Sesion no valida."):
        r = require_login()
        if r:
            return jsonify({"ok": False, "error": message}), 401
        return None

    def launcher_exit_signal_path() -> Path | None:
        raw = os.getenv("LAUNCHER_EXIT_SIGNAL", "").strip()
        if not raw:
            return None
        try:
            return Path(raw)
        except (TypeError, ValueError):
            return None

    @app.before_request
    def inject_user_context():
        if (request.args.get("launcher") or "").strip() == "1":
            session["launched_by_launcher"] = "1"
        u = current_user_row()
        g.current_user = u
        g.is_admin = is_admin(u)

    def inventario_redirect_with_filters(producto_id: str | None = None):
        q = (request.form.get("f_q") or request.args.get("q") or "").strip()
        g = (request.form.get("f_g") or request.args.get("g") or "").strip()
        low = "1" if (request.form.get("f_low") or request.args.get("low")) == "1" else "0"
        page = parse_positive_int(request.form.get("f_page") or request.args.get("page"), 1)

        params = {}
        if producto_id:
            params["producto_id"] = producto_id
        if q:
            params["q"] = q
        if g:
            params["g"] = g
        if low == "1":
            params["low"] = "1"
        if page > 1:
            params["page"] = page

        return redirect(url_for("inventario", **params))

    def list_filters_from_request() -> dict[str, str]:
        return {
            "q": (request.form.get("f_q") or request.args.get("q") or "").strip(),
            "g": (request.form.get("f_g") or request.args.get("g") or "").strip(),
        }

    def redirect_with_list_filters(endpoint: str):
        current = list_filters_from_request()
        params = {}
        if current["q"]:
            params["q"] = current["q"]
        if current["g"]:
            params["g"] = current["g"]
        return redirect(url_for(endpoint, **params))

    def history_redirect_params() -> dict[str, str]:
        params = {}
        for key in ("q", "from", "to"):
            value = (request.form.get(key) or request.args.get(key) or "").strip()
            if value:
                params[key] = value
        return params

    def wants_cash_update() -> bool:
        return (request.form.get("update_cash") or "").strip().lower() in {"1", "true", "yes", "si", "sí", "on"}

    def simple_browser_state(endpoint: str, current_filters: dict[str, str], total: int) -> dict[str, object]:
        return {
            "page": 1,
            "page_count": 1,
            "per_page": total,
            "total": total,
            "shown_from": 1 if total else 0,
            "shown_to": total,
            "has_prev": False,
            "has_next": False,
            "prev_url": "",
            "next_url": "",
            "current_url": url_for(
                endpoint,
                **{key: value for key, value in current_filters.items() if value},
            ),
        }

    def is_partial_json_request() -> bool:
        return (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            and (request.args.get("format") or "").strip().lower() == "json"
        )

    def product_browser_summary(browser: dict[str, object], empty_message: str, item_label: str = "productos") -> str:
        total = int(browser.get("total") or 0)
        if total <= 0:
            return empty_message
        shown_from = int(browser.get("shown_from") or 0)
        shown_to = int(browser.get("shown_to") or 0)
        return f"Mostrando {shown_from}-{shown_to} de {total} {item_label}."

    def parse_decimal(raw: str) -> float:
        t = (raw or "").strip().replace(" ", "")
        if t == "":
            raise ValueError

        if "," in t and "." in t:
            if t.rfind(",") > t.rfind("."):
                t = t.replace(".", "").replace(",", ".")
            else:
                t = t.replace(",", "")
        else:
            t = t.replace(",", ".")

        return float(t)

    def safe_float(raw: object) -> float:
        text = str(raw or "").strip().replace(" ", "")
        if not text:
            return 0.0
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return 0.0

    def format_compact_number(value: float) -> str:
        if abs(value - int(value)) < 1e-9:
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def normalize_text_search(raw: object) -> str:
        text = unicodedata.normalize("NFD", str(raw or ""))
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
        return "".join(text.lower().split())

    def find_default_invoice_product(products: list[dict[str, str]]) -> dict[str, str] | None:
        target_name = normalize_text_search("tasa por comensal")
        for product in products:
            if (
                normalize_text_search(product.get("nombre", "")) == target_name
                and (product.get("stock_infinito") or "").strip() == "1"
            ):
                return product
        return None

    def parse_positive_int(raw: str | None, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
        try:
            value = int(str(raw or "").strip())
        except ValueError:
            value = default
        if value < minimum:
            value = minimum
        if maximum is not None and value > maximum:
            value = maximum
        return value

    def paginate_rows(
        rows: list[dict[str, object]],
        *,
        per_page: int,
        endpoint: str,
        current_filters: dict[str, str],
        extra_params: dict[str, object] | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        total = len(rows)
        page_count = max(1, math.ceil(total / per_page)) if per_page > 0 else 1
        current_page = parse_positive_int(request.args.get("page"), 1, maximum=page_count)
        start = 0 if per_page <= 0 else (current_page - 1) * per_page
        end = None if per_page <= 0 else start + per_page
        page_rows = rows[start:end]

        def page_url(page: int) -> str:
            params: dict[str, object] = {}
            for key, value in current_filters.items():
                if key == "low":
                    if value == "1":
                        params[key] = value
                    continue
                if value:
                    params[key] = value
            if page > 1:
                params["page"] = page
            if extra_params:
                for key, value in extra_params.items():
                    if value not in (None, ""):
                        params[key] = value
            return url_for(endpoint, **params)

        shown_from = start + 1 if total and page_rows else 0
        shown_to = start + len(page_rows)
        return page_rows, {
            "page": current_page,
            "page_count": page_count,
            "per_page": per_page,
            "total": total,
            "shown_from": shown_from,
            "shown_to": shown_to,
            "has_prev": current_page > 1,
            "has_next": current_page < page_count,
            "prev_url": page_url(current_page - 1) if current_page > 1 else "",
            "next_url": page_url(current_page + 1) if current_page < page_count else "",
            "current_url": page_url(current_page),
        }

    def stock_unit_label(product: dict[str, str]) -> str:
        return (product.get("unidad") or "ud").strip() or "ud"

    def sale_unit_label(product: dict[str, str]) -> str:
        return product_sale_unit(product)

    def fraction_info_label(product: dict[str, str]) -> str:
        if not product_is_fractionable(product):
            return ""
        fractions = format_compact_number(product_fraction_count(product))
        return f"{fractions} {sale_unit_label(product)} por {stock_unit_label(product)}"

    def stock_display_parts(product: dict[str, str], stock_value: float | None) -> tuple[str, str]:
        if stock_value is None:
            return ("♾️", "")

        stock_text = f"{format_compact_number(stock_value)} {stock_unit_label(product)}"
        if not product_is_fractionable(product):
            return (stock_text, "")

        sale_qty = stock_quantity_to_sale_quantity(product, stock_value)
        sale_text = f"{format_compact_number(sale_qty)} {sale_unit_label(product)}"
        return (sale_text, stock_text)

    def stored_group_name(raw: object) -> str:
        return str(raw or "").strip()

    def display_group_name(raw: object) -> str:
        return stored_group_name(raw) or "Sin grupo"

    def display_group_emoji(raw: object) -> str:
        return str(raw or "").strip() or str(get_emoji_entry("question")["char"])

    def normalize_group_filter_value(raw: object) -> str | None:
        value = str(raw or "").strip()
        if not value:
            return None
        if value == "__ungrouped__":
            return ""
        return value

    def group_options_for_products(prods: list[dict[str, str]] | None = None) -> list[tuple[str, str]]:
        sync_group_catalog_safe(products=prods)
        try:
            return [
                (
                    stored_group_name(row.get("nombre")),
                    (row.get("emoji") or "package").strip() or "package",
                )
                for row in list_groups(config.CSV_GRUPOS)
            ]
        except Exception as exc:
            print(
                f"[BuenYantar] Aviso: no se pudieron leer los grupos configurados: {exc}",
                file=sys.stderr,
            )
            return []

    def extract_note_value(note: str, prefix: str) -> str:
        for part in (note or "").split("|"):
            text = part.strip()
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return ""

    def parse_iso_datetime(raw: str) -> datetime | None:
        text = (raw or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def matches_date_window(raw: str, from_date: str, to_date: str) -> bool:
        if not from_date and not to_date:
            return True

        parsed = parse_iso_datetime(raw)
        if not parsed:
            return False

        value = parsed.date()
        if from_date:
            try:
                if value < date.fromisoformat(from_date):
                    return False
            except ValueError:
                pass
        if to_date:
            try:
                if value > date.fromisoformat(to_date):
                    return False
            except ValueError:
                pass
        return True

    def make_csv_response(filename: str, headers: list[str], rows: list[dict[str, object]]) -> Response:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in headers})

        response = Response(buffer.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def config_section_key(section: str = "") -> str:
        aliases = {
            "": "caja",
            "saldo-caja": "caja",
            "caja": "caja",
            "usuarios-admin": "usuarios",
            "perfil-acceso": "usuarios",
            "usuarios": "usuarios",
            "grupos-productos": "grupos",
            "grupos": "grupos",
            "migracion-legacy": "migracion",
            "migracion": "migracion",
            "pantalla": "pantalla",
            "zoom": "pantalla",
            "logs-uso": "logs",
            "logs": "logs",
        }
        return aliases.get((section or "").strip().lower(), "caja")

    def redirect_to_config(section: str = "", **values):
        endpoint_by_section = {
            "caja": "configuracion_caja",
            "usuarios": "configuracion_usuarios",
            "grupos": "configuracion_grupos",
            "migracion": "configuracion_migracion",
            "pantalla": "configuracion_pantalla",
            "logs": "configuracion_logs",
        }
        key = config_section_key(section)
        return redirect(url_for(endpoint_by_section[key], **values))

    def page_limit(normal: int, low_resource: int) -> int:
        return low_resource if config.LOW_RESOURCE_MODE else normal

    def config_shortcuts(active_section: str) -> list[dict[str, str | bool]]:
        items = [
            ("caja", "Configurar saldo de caja", url_for("configuracion_caja")),
            ("usuarios", "Configurar usuarios", url_for("configuracion_usuarios")),
            ("grupos", "Gestionar grupos", url_for("configuracion_grupos")),
            ("migracion", "Migracion legacy", url_for("configuracion_migracion")),
            ("pantalla", "Pantalla y zoom", url_for("configuracion_pantalla")),
            ("logs", "Revisar logs de uso", url_for("configuracion_logs")),
        ]
        return [
            {
                "key": key,
                "label": label,
                "href": href,
                "active": key == active_section,
            }
            for key, label, href in items
        ]

    def redirect_to_user_area():
        if user_is_admin():
            return redirect_to_config("usuarios")
        return redirect(url_for("gestion_usuarios"))

    def admin_logs_context() -> dict[str, object]:
        all_logs = list_logs(config.CSV_LOGS, limit=page_limit(2000, 500))
        logs = list(all_logs)
        current_q = (request.args.get("q") or "").strip()
        current_user = (request.args.get("user") or "").strip()
        current_from = (request.args.get("from") or "").strip()
        current_to = (request.args.get("to") or "").strip()
        q = current_q.lower()
        user = current_user.lower()

        if q:
            logs = [
                row for row in logs
                if q in (row.get("accion", "").lower()) or q in (row.get("detalle", "").lower())
            ]
        if user:
            logs = [row for row in logs if user in (row.get("usuario", "").lower())]
        if current_from or current_to:
            logs = [
                row for row in logs
                if matches_date_window(row.get("fecha", ""), current_from, current_to)
            ]

        users = sorted({(row.get("usuario") or "").strip() for row in all_logs if (row.get("usuario") or "").strip()})
        cutoff = datetime.now() - timedelta(hours=24)
        recent_logs = 0
        error_logs = 0
        for row in logs:
            detail = (row.get("detalle") or "").lower()
            if "status=4" in detail or "status=5" in detail:
                error_logs += 1
            parsed = parse_iso_datetime(row.get("fecha", ""))
            if parsed and parsed >= cutoff:
                recent_logs += 1

        return {
            "logs": logs,
            "current_q": current_q,
            "current_user": current_user,
            "current_from": current_from,
            "current_to": current_to,
            "user_options": users,
            "log_summary": {
                "shown": len(logs),
                "users": len({(row.get("usuario") or "").strip() for row in logs if (row.get("usuario") or "").strip()}),
                "recent": recent_logs,
                "errors": error_logs,
            },
        }

    def admin_user_context() -> dict[str, object]:
        users = list_users(config.CSV_USUARIOS)
        return {
            "users": users,
            "user_stats": {
                "total": len(users),
                "admins": sum(1 for row in users if (row.get("rol") or "").strip().lower() == "admin"),
                "normal": sum(1 for row in users if (row.get("rol") or "normal").strip().lower() != "admin"),
            },
        }

    def admin_groups_context() -> dict[str, object]:
        products = get_products(config.CSV_PRODUCTOS)
        sync_group_catalog_safe(products=products)
        counts: dict[str, int] = {}
        for product in products:
            group_name = stored_group_name(product.get("grupo"))
            if not group_name:
                continue
            counts[group_name] = counts.get(group_name, 0) + 1

        groups = []
        try:
            source_groups = list_groups(config.CSV_GRUPOS)
        except Exception as exc:
            print(
                f"[BuenYantar] Aviso: no se pudo construir la vista de grupos: {exc}",
                file=sys.stderr,
            )
            source_groups = []

        for row in source_groups:
            group_name = stored_group_name(row.get("nombre"))
            groups.append({
                **row,
                "product_count": counts.get(group_name, 0),
            })
        groups.sort(key=lambda row: (row.get("nombre") or "").lower())

        return {
            "groups": groups,
            "group_stats": {
                "total": len(groups),
                "used": sum(1 for row in groups if row["product_count"] > 0),
                "empty": sum(1 for row in groups if row["product_count"] == 0),
                "products": len(products),
            },
        }

    def inventory_snapshot(
        prods: list[dict[str, str]] | None = None,
        stock_map: dict[str, float] | None = None,
    ) -> dict[str, int]:
        prods = prods if prods is not None else get_products(config.CSV_PRODUCTOS)
        stock_map = stock_map if stock_map is not None else calc_stock_by_product(config.CSV_MOVS)
        low_stock = 0
        infinite = 0
        groups: set[str] = set()

        for p in prods:
            group_name = stored_group_name(p.get("grupo"))
            if group_name:
                groups.add(group_name)
            if p.get("stock_infinito", "0") == "1":
                infinite += 1
                continue
            current_stock = stock_map.get(p.get("producto_id", ""), 0.0)
            stock_min = safe_float(p.get("stock_minimo"))
            if current_stock < stock_min:
                low_stock += 1

        return {
            "total_products": len(prods),
            "low_stock": low_stock,
            "infinite_stock": infinite,
            "groups": len(groups),
        }

    def format_money(value: float) -> str:
        return f"{value:.2f} €"

    def format_signed_money(value: float) -> str:
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f} €"

    def format_percent(value: float) -> str:
        return f"{value:.1f}%".replace(".0%", "%")

    def format_analysis_date(value: date) -> str:
        month_names = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
        return f"{value.day} {month_names[value.month - 1]}"

    def parse_query_date(raw: str, fallback: date) -> date:
        text = (raw or "").strip()
        if not text:
            return fallback
        try:
            return date.fromisoformat(text)
        except ValueError:
            return fallback

    def analysis_detail_range_defaults() -> tuple[date, date]:
        end_date = date.today()
        start_date = date(end_date.year, 1, 1)
        return start_date, end_date

    def analysis_detail_type_options() -> list[dict[str, str]]:
        return [
            {"value": "ancla", "label": "Ancla"},
            {"value": "factura", "label": "Factura"},
            {"value": "compra", "label": "Compra"},
            {"value": "gasto", "label": "Gasto"},
            {"value": "ajuste", "label": "Ajuste"},
        ]

    def normalize_analysis_detail_types(raw_values: list[str] | tuple[str, ...]) -> list[str]:
        allowed_order = [item["value"] for item in analysis_detail_type_options()]
        allowed = set(allowed_order)
        seen: set[str] = set()
        normalized: list[str] = []

        for raw in raw_values:
            for part in str(raw or "").split(","):
                value = part.strip().lower()
                if value and value in allowed and value not in seen:
                    normalized.append(value)
                    seen.add(value)

        normalized.sort(key=lambda value: allowed_order.index(value))
        return normalized

    def analysis_detail_query_params(
        start_date: date,
        end_date: date,
        selected_types: list[str],
        view: str = "detalle",
    ) -> dict[str, object]:
        params: dict[str, object] = {
            "view": view,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        }
        if selected_types:
            params["tipo"] = selected_types
        return params

    def analysis_available_years(cash_movements_csv) -> list[int]:
        years = {date.today().year}
        for row in read_all(cash_movements_csv):
            parsed = parse_iso_datetime(row.get("fecha", ""))
            if parsed:
                years.add(parsed.year)
        return sorted(years, reverse=True)

    def parse_analysis_year(raw: str, fallback: int, allowed_years: list[int]) -> int:
        text = (raw or "").strip()
        if not text:
            return fallback
        try:
            value = int(text)
        except ValueError:
            return fallback
        if value in allowed_years:
            return value
        return fallback

    def analysis_annual_range(selected_year: int) -> tuple[date, date]:
        start_date = date(selected_year, 1, 1)
        return start_date, date(selected_year, 12, 31)

    def build_analysis_annual_summary(selected_year: int) -> dict[str, object]:
        annual_start_date, annual_end_date = analysis_annual_range(selected_year)
        annual_summary_raw = cash_annual_summary(
            config.CSV_CAJA_MOVIMIENTOS,
            start_date=annual_start_date,
            end_date=annual_end_date,
        )
        annual_summary_rows = [
            {
                "label": analysis_bucket_label(date(int(row["year"]), int(row["month"]), 1), "month"),
                "income_total": float(row["income_total"]),
                "expense_total": float(row["expense_total"]),
                "saldo": float(row["saldo"]),
            }
            for row in annual_summary_raw["rows"]
        ]
        return {
            "rows": annual_summary_rows,
            "opening_balance": float(annual_summary_raw["opening_balance"]),
            "closing_balance": float(annual_summary_raw["closing_balance"]),
            "net_balance": float(annual_summary_raw["net_balance"]),
        }

    def build_analysis_annual_markdown(selected_year: int, annual_summary: dict[str, object]) -> str:
        lines = [
            f"# Resumen anual de caja {selected_year}",
            "",
            "## Resumen mensual",
            "",
            "| Mes | Total ingresos | Total gastos | Saldo |",
            "| --- | -------------:| -----------:| -----:|",
        ]

        for row in annual_summary["rows"]:
            lines.append(
                f"| {row['label']} | "
                f"{float(row['income_total']):+.2f} € | "
                f"{-float(row['expense_total']):.2f} € | "
                f"{float(row['saldo']):+.2f} € |"
            )

        lines.extend([
            "",
            "## KPIs",
            "",
            f"- Saldo inicial: {float(annual_summary['opening_balance']):.2f} €",
            f"- Saldo final: {float(annual_summary['closing_balance']):.2f} €",
            f"- Balance saldo de caja: {float(annual_summary['net_balance']):+.2f} €",
        ])
        return "\n".join(lines)

    def analysis_range_defaults() -> tuple[date, date]:
        end_date = date.today()
        start_date = end_date - timedelta(days=29)
        return start_date, end_date

    def analysis_bucket_kind(start_date: date, end_date: date) -> str:
        days = max((end_date - start_date).days + 1, 1)
        if days <= 31:
            return "day"
        if days <= 150:
            return "week"
        return "month"

    def analysis_bucket_key(value_date: date, kind: str) -> date:
        if kind == "month":
            return date(value_date.year, value_date.month, 1)
        if kind == "week":
            return value_date - timedelta(days=value_date.weekday())
        return value_date

    def analysis_bucket_label(value_date: date, kind: str) -> str:
        if kind == "month":
            month_names = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
            return f"{month_names[value_date.month - 1]} {value_date.year}"
        if kind == "week":
            end_week = value_date + timedelta(days=6)
            return f"{format_analysis_date(value_date)} - {format_analysis_date(end_week)}"
        return format_analysis_date(value_date)

    def analysis_in_range(raw: str, start_date: date, end_date: date) -> bool:
        parsed = parse_iso_datetime(raw)
        if not parsed:
            return False
        value = parsed.date()
        return start_date <= value <= end_date

    def analysis_percent(value: float, max_value: float) -> float:
        if max_value <= 0:
            return 0.0
        return round((value / max_value) * 100, 1)

    def analysis_delta_note(current: float, previous: float, *, inverted: bool = False) -> str:
        if abs(previous) < 1e-9:
            if abs(current) < 1e-9:
                return "Sin cambios frente al periodo anterior"
            return "Sin datos comparables en el periodo anterior"

        delta = ((current - previous) / previous) * 100
        good = delta <= 0 if inverted else delta >= 0
        prefix = "Mejora" if good else "Empeora"
        sign = "+" if delta >= 0 else ""
        return f"{prefix} {sign}{format_percent(delta)} vs periodo anterior"

    def analysis_stat_card(label: str, value: str, note: str, tone: str = "neutral") -> dict[str, str]:
        return {
            "label": label,
            "value": value,
            "note": note,
            "tone": tone,
        }

    def analysis_ranking_rows(
        items: list[dict[str, object]],
        *,
        value_key: str,
        value_formatter,
        meta_builder,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        if not items:
            return []

        top_items = items[:limit]
        max_value = max(float(item.get(value_key) or 0.0) for item in top_items) or 1.0
        rows: list[dict[str, object]] = []
        for item in top_items:
            raw_value = float(item.get(value_key) or 0.0)
            rows.append({
                "label": item.get("label", "-"),
                "meta": meta_builder(item),
                "value": value_formatter(raw_value),
                "pct": analysis_percent(raw_value, max_value),
            })
        return rows

    def analysis_stock_alerts() -> list[dict[str, object]]:
        products = get_products(config.CSV_PRODUCTOS)
        stock_map = calc_stock_by_product(config.CSV_MOVS)
        alerts: list[dict[str, object]] = []
        for product in products:
            if product.get("stock_infinito", "0") == "1":
                continue
            product_id = product.get("producto_id", "")
            current_stock = stock_map.get(product_id, 0.0)
            min_stock = safe_float(product.get("stock_minimo"))
            gap = min_stock - current_stock
            if gap <= 0:
                continue
            alerts.append({
                "label": f"{display_group_emoji(product.get('grupo_emoji'))} {product.get('nombre', '')}",
                "meta": f"Actual {format_compact_number(current_stock)} · Minimo {format_compact_number(min_stock)}",
                "value": f"Faltan {format_compact_number(gap)}",
                "pct": 100.0 if min_stock <= 0 else min(100.0, round((gap / min_stock) * 100, 1)),
                "href": url_for("inventario", producto_id=product_id, low="1"),
            })
        alerts.sort(key=lambda row: row["pct"], reverse=True)
        return alerts[:8]

    def analysis_context(start_date: date, end_date: date, current_tab: str) -> dict[str, object]:
        period_days = max((end_date - start_date).days + 1, 1)
        previous_end = start_date - timedelta(days=1)
        previous_start = previous_end - timedelta(days=period_days - 1)

        products = get_products(config.CSV_PRODUCTOS)
        product_map = {product.get("producto_id", ""): product for product in products}

        invoices_all = list_invoices(config.CSV_FACTURAS, limit=0)
        invoice_lines_all = list_invoice_lines(config.CSV_FACTURA_LINEAS)
        purchase_tickets_all, purchase_lines_map_all = list_purchase_history(
            config.CSV_MOVS,
            product_names={pid: product.get("nombre", "") for pid, product in product_map.items()},
            limit=0,
        )
        expenses_all = list_free_expenses(config.CSV_GASTOS_LIBRES, limit=0)

        invoices = [row for row in invoices_all if analysis_in_range(row.get("fecha", ""), start_date, end_date)]
        purchases = [row for row in purchase_tickets_all if analysis_in_range(row.get("fecha", ""), start_date, end_date)]
        expenses = [row for row in expenses_all if analysis_in_range(row.get("fecha", ""), start_date, end_date)]

        invoice_ids = {(row.get("factura_id") or "").strip() for row in invoices if (row.get("factura_id") or "").strip()}
        filtered_invoice_lines = [
            line for line in invoice_lines_all
            if (line.get("factura_id") or "").strip() in invoice_ids
        ]

        invoice_amount = sum(safe_float(row.get("total_importe")) for row in invoices)
        purchase_amount = sum(safe_float(row.get("total_importe")) for row in purchases)
        expense_amount = sum(safe_float(row.get("importe")) for row in expenses)
        net_amount = invoice_amount - purchase_amount - expense_amount
        gross_margin_amount = invoice_amount - purchase_amount

        invoice_count = len(invoices)
        purchase_count = len(purchases)
        expense_count = len(expenses)
        total_movements = invoice_count + purchase_count + expense_count
        client_count = len({(row.get("cliente") or "").strip() for row in invoices if (row.get("cliente") or "").strip()})
        provider_count = len({(row.get("proveedor") or "").strip() for row in purchases if (row.get("proveedor") or "").strip()})

        def analysis_ratio(part: float, whole: float) -> float:
            if abs(whole) < 1e-9:
                return 0.0
            return (part / whole) * 100

        sales_by_product: dict[str, dict[str, object]] = {}
        sales_by_group: dict[str, dict[str, object]] = {}
        for line in filtered_invoice_lines:
            product_id = (line.get("producto_id") or "").strip()
            product = product_map.get(product_id, {})
            label = f"{display_group_emoji(product.get('grupo_emoji'))} {line.get('producto_nombre', '') or product.get('nombre', product_id)}"
            qty = safe_float(line.get("cantidad"))
            amount = safe_float(line.get("importe_linea"))
            group_name = display_group_name(product.get("grupo"))
            group_emoji = display_group_emoji(product.get("grupo_emoji"))

            product_row = sales_by_product.setdefault(product_id or label, {
                "label": label,
                "qty": 0.0,
                "amount": 0.0,
                "lines": 0,
            })
            product_row["qty"] += qty
            product_row["amount"] += amount
            product_row["lines"] += 1

            group_row = sales_by_group.setdefault(group_name, {
                "label": f"{group_emoji} {group_name}",
                "sales_amount": 0.0,
                "sales_qty": 0.0,
                "purchase_amount": 0.0,
                "purchase_qty": 0.0,
            })
            group_row["sales_amount"] += amount
            group_row["sales_qty"] += qty

        spend_by_product: dict[str, dict[str, object]] = {}
        spend_by_group: dict[str, dict[str, object]] = {}
        for ticket in purchases:
            ref_id = (ticket.get("ref_id") or "").strip()
            for line in purchase_lines_map_all.get(ref_id, []):
                product_id = (line.get("producto_id") or "").strip()
                product = product_map.get(product_id, {})
                label = f"{display_group_emoji(product.get('grupo_emoji'))} {line.get('producto_nombre', '') or product.get('nombre', product_id)}"
                qty = safe_float(line.get("cantidad"))
                amount = safe_float(line.get("importe_linea"))
                group_name = display_group_name(product.get("grupo"))
                group_emoji = display_group_emoji(product.get("grupo_emoji"))

                product_row = spend_by_product.setdefault(product_id or label, {
                    "label": label,
                    "qty": 0.0,
                    "amount": 0.0,
                    "lines": 0,
                })
                product_row["qty"] += qty
                product_row["amount"] += amount
                product_row["lines"] += 1

                group_row = spend_by_group.setdefault(group_name, {
                    "label": f"{group_emoji} {group_name}",
                    "sales_amount": 0.0,
                    "sales_qty": 0.0,
                    "purchase_amount": 0.0,
                    "purchase_qty": 0.0,
                })
                group_row["purchase_amount"] += amount
                group_row["purchase_qty"] += qty

        expense_by_category: dict[str, dict[str, object]] = {}
        for expense in expenses:
            category = (expense.get("categoria") or "Sin categoria").strip() or "Sin categoria"
            row = expense_by_category.setdefault(category, {
                "label": category,
                "amount": 0.0,
                "count": 0,
            })
            row["amount"] += safe_float(expense.get("importe"))
            row["count"] += 1

        user_activity: dict[str, dict[str, object]] = {}
        for invoice in invoices:
            user = (invoice.get("usuario") or "").strip() or "-"
            row = user_activity.setdefault(user, {
                "label": user,
                "invoice_amount": 0.0,
                "purchase_amount": 0.0,
                "expense_amount": 0.0,
                "invoice_count": 0,
                "purchase_count": 0,
                "expense_count": 0,
            })
            row["invoice_amount"] += safe_float(invoice.get("total_importe"))
            row["invoice_count"] += 1
        for purchase in purchases:
            user = (purchase.get("usuario") or "").strip() or "-"
            row = user_activity.setdefault(user, {
                "label": user,
                "invoice_amount": 0.0,
                "purchase_amount": 0.0,
                "expense_amount": 0.0,
                "invoice_count": 0,
                "purchase_count": 0,
                "expense_count": 0,
            })
            row["purchase_amount"] += safe_float(purchase.get("total_importe"))
            row["purchase_count"] += 1
        for expense in expenses:
            user = (expense.get("usuario") or "").strip() or "-"
            row = user_activity.setdefault(user, {
                "label": user,
                "invoice_amount": 0.0,
                "purchase_amount": 0.0,
                "expense_amount": 0.0,
                "invoice_count": 0,
                "purchase_count": 0,
                "expense_count": 0,
            })
            row["expense_amount"] += safe_float(expense.get("importe"))
            row["expense_count"] += 1

        client_totals: dict[str, dict[str, object]] = {}
        for invoice in invoices:
            client = (invoice.get("cliente") or "").strip()
            if not client:
                continue
            row = client_totals.setdefault(client, {"label": client, "amount": 0.0, "count": 0})
            row["amount"] += safe_float(invoice.get("total_importe"))
            row["count"] += 1

        provider_totals: dict[str, dict[str, object]] = {}
        for purchase in purchases:
            provider = (purchase.get("proveedor") or "").strip()
            if not provider:
                continue
            row = provider_totals.setdefault(provider, {"label": provider, "amount": 0.0, "count": 0})
            row["amount"] += safe_float(purchase.get("total_importe"))
            row["count"] += 1

        bucket_kind = analysis_bucket_kind(start_date, end_date)
        flow_map: dict[date, dict[str, float | str]] = {}

        def ensure_bucket(raw: str) -> dict[str, float | str]:
            parsed = parse_iso_datetime(raw)
            bucket_date = analysis_bucket_key((parsed.date() if parsed else start_date), bucket_kind)
            if bucket_date not in flow_map:
                flow_map[bucket_date] = {
                    "label": analysis_bucket_label(bucket_date, bucket_kind),
                    "incoming": 0.0,
                    "outgoing": 0.0,
                    "net": 0.0,
                }
            return flow_map[bucket_date]

        for invoice in invoices:
            bucket = ensure_bucket(invoice.get("fecha", ""))
            amount = safe_float(invoice.get("total_importe"))
            bucket["incoming"] += amount
            bucket["net"] += amount
        for purchase in purchases:
            bucket = ensure_bucket(purchase.get("fecha", ""))
            amount = safe_float(purchase.get("total_importe"))
            bucket["outgoing"] += amount
            bucket["net"] -= amount
        for expense in expenses:
            bucket = ensure_bucket(expense.get("fecha", ""))
            amount = safe_float(expense.get("importe"))
            bucket["outgoing"] += amount
            bucket["net"] -= amount

        flow_rows = [
            {
                "label": data["label"],
                "incoming": float(data["incoming"]),
                "outgoing": float(data["outgoing"]),
                "net": float(data["net"]),
            }
            for _, data in sorted(flow_map.items())
        ]
        max_flow_value = max(
            [row["incoming"] for row in flow_rows] + [row["outgoing"] for row in flow_rows] + [1.0]
        )
        for row in flow_rows:
            row["incoming_width"] = analysis_percent(row["incoming"], max_flow_value)
            row["outgoing_width"] = analysis_percent(row["outgoing"], max_flow_value)
            row["net_label"] = format_signed_money(row["net"])
            row["incoming_label"] = format_money(row["incoming"])
            row["outgoing_label"] = format_money(row["outgoing"])
            row["net_class"] = "positive" if row["net"] > 0 else "negative" if row["net"] < 0 else "neutral"

        flow_chart_rows = flow_rows[-8:]
        flow_chart_points = ""
        flow_chart_baseline = 50.0
        if flow_chart_rows:
            chart_values = [row["net"] for row in flow_chart_rows]
            chart_min = min(chart_values + [0.0])
            chart_max = max(chart_values + [0.0])
            chart_span = (chart_max - chart_min) or 1.0
            point_values = []
            for index, row in enumerate(flow_chart_rows):
                x = 50.0 if len(flow_chart_rows) == 1 else (index / (len(flow_chart_rows) - 1)) * 100
                y = 100 - (((row["net"] - chart_min) / chart_span) * 100)
                point_values.append(f"{x:.2f},{y:.2f}")
            flow_chart_points = " ".join(point_values)
            flow_chart_baseline = 100 - (((0.0 - chart_min) / chart_span) * 100)

        outflow_items = [
            ("Compras", purchase_amount, "#5c8fda"),
            ("Gastos", expense_amount, "#dd7f66"),
        ]
        outflow_total = sum(value for _, value, _ in outflow_items)
        outflow_segments = []
        outflow_legend = []
        outflow_cursor = 0.0
        for label, value, color in outflow_items:
            pct = (value / outflow_total * 100) if outflow_total > 0 else 0.0
            outflow_segments.append(f"{color} {outflow_cursor:.2f}% {outflow_cursor + pct:.2f}%")
            outflow_legend.append({
                "label": label,
                "value": format_money(value),
                "pct": format_percent(pct),
                "color": color,
            })
            outflow_cursor += pct
        outflow_style = "background: conic-gradient(" + (", ".join(outflow_segments) if outflow_segments else "#dbe7e1 0 100%") + ");"

        sales_items = sorted(sales_by_product.values(), key=lambda row: (row["amount"], row["qty"]), reverse=True)
        spend_items = sorted(spend_by_product.values(), key=lambda row: (row["amount"], row["qty"]), reverse=True)
        sold_units = sum(float(row.get("qty") or 0.0) for row in sales_items)
        purchased_units = sum(float(row.get("qty") or 0.0) for row in spend_items)
        category_items_map = dict(sales_by_group)
        for key, value in spend_by_group.items():
            if key not in category_items_map:
                category_items_map[key] = value
        category_items = sorted(
            category_items_map.values(),
            key=lambda row: (row["sales_amount"] + row["purchase_amount"], row["sales_qty"] + row["purchase_qty"]),
            reverse=True,
        )

        max_category_sales = max([float(row.get("sales_amount") or 0.0) for row in category_items] + [1.0])
        max_category_spend = max([float(row.get("purchase_amount") or 0.0) for row in category_items] + [1.0])
        category_rows = []
        for item in category_items[:8]:
            sales_amount = float(item.get("sales_amount") or 0.0)
            purchase_amount = float(item.get("purchase_amount") or 0.0)
            net_amount_by_group = sales_amount - purchase_amount
            category_rows.append({
                "label": item.get("label", "-"),
                "sales_value": format_money(sales_amount),
                "purchase_value": format_money(purchase_amount),
                "sales_width": analysis_percent(sales_amount, max_category_sales),
                "purchase_width": analysis_percent(purchase_amount, max_category_spend),
                "net_value": format_signed_money(net_amount_by_group),
                "net_class": "profit" if net_amount_by_group >= 0 else "loss",
            })

        product_margin_items = []
        product_margin_map = dict(sales_by_product)
        for key, value in spend_by_product.items():
            if key not in product_margin_map:
                product_margin_map[key] = value
        for key, item in product_margin_map.items():
            sale = sales_by_product.get(key, {})
            spend = spend_by_product.get(key, {})
            sales_amount = float(sale.get("amount") or 0.0)
            purchase_cost = float(spend.get("amount") or 0.0)
            margin_value = sales_amount - purchase_cost
            product_margin_items.append({
                "label": sale.get("label") or spend.get("label") or item.get("label") or "-",
                "sales_amount": sales_amount,
                "purchase_amount": purchase_cost,
                "margin": margin_value,
            })
        product_margin_items = sorted(
            product_margin_items,
            key=lambda row: (row["margin"], row["sales_amount"]),
            reverse=True,
        )
        max_margin_value = max([abs(float(row["margin"])) for row in product_margin_items[:8]] + [1.0])
        product_margin_rows = [
            {
                "label": row["label"],
                "value": format_signed_money(float(row["margin"])),
                "meta": (
                    f"Ventas {format_money(float(row['sales_amount']))} · "
                    f"Compras {format_money(float(row['purchase_amount']))}"
                ),
                "pct": analysis_percent(abs(float(row["margin"])), max_margin_value),
                "tone": "profit" if float(row["margin"]) >= 0 else "loss",
            }
            for row in product_margin_items[:8]
        ]

        top_sale_item = sales_items[0] if sales_items else None
        current_low_stock = analysis_stock_alerts()
        average_ticket = invoice_amount / invoice_count if invoice_count else 0.0
        top_margin_item = product_margin_items[0] if product_margin_items else None
        top_client_item = max(client_totals.values(), key=lambda row: row["amount"], default=None)
        top_provider_item = max(provider_totals.values(), key=lambda row: row["amount"], default=None)

        tabs = [
            {
                "key": "resumen",
                "label": "Resumen",
                "href": url_for("analisis", tab="resumen", **{"from": start_date.isoformat(), "to": end_date.isoformat()}),
                "active": current_tab == "resumen",
            },
            {
                "key": "productos",
                "label": "Productos",
                "href": url_for("analisis", tab="productos", **{"from": start_date.isoformat(), "to": end_date.isoformat()}),
                "active": current_tab == "productos",
            },
            {
                "key": "operativa",
                "label": "Operativa",
                "href": url_for("analisis", tab="operativa", **{"from": start_date.isoformat(), "to": end_date.isoformat()}),
                "active": current_tab == "operativa",
            },
        ]

        preset_specs = [
            ("30d", "30 dias", date.today() - timedelta(days=29), date.today()),
            ("90d", "90 dias", date.today() - timedelta(days=89), date.today()),
            ("mes", "Este mes", date.today().replace(day=1), date.today()),
            ("ano", "Este ano", date(date.today().year, 1, 1), date.today()),
        ]
        presets = [
            {
                "label": label,
                "href": url_for("analisis", tab=current_tab, **{"from": start.isoformat(), "to": end.isoformat()}),
                "active": start == start_date and end == end_date,
            }
            for _, label, start, end in preset_specs
        ]

        range_label = f"{format_analysis_date(start_date)} - {format_analysis_date(end_date)}"
        previous_label = f"{format_analysis_date(previous_start)} - {format_analysis_date(previous_end)}"

        overview_stats = [
            analysis_stat_card(
                "Ingresos facturados",
                format_money(invoice_amount),
                f"{invoice_count} facturas · ticket medio {format_money(average_ticket)}",
                "positive",
            ),
            analysis_stat_card(
                "Compras registradas",
                format_money(purchase_amount),
                f"{format_percent(analysis_ratio(purchase_amount, invoice_amount))} de los ingresos del periodo",
                "negative",
            ),
            analysis_stat_card(
                "Gastos libres",
                format_money(expense_amount),
                f"{format_percent(analysis_ratio(expense_amount, invoice_amount))} de los ingresos del periodo",
                "negative",
            ),
            analysis_stat_card(
                "Balance neto",
                format_signed_money(net_amount),
                f"Margen neto {format_percent(analysis_ratio(net_amount, invoice_amount))}",
                "positive" if net_amount >= 0 else "negative",
            ),
        ]

        product_stats = [
            analysis_stat_card(
                "Productos vendidos",
                str(len(sales_items)),
                f"{sold_units:.0f} uds facturadas",
            ),
            analysis_stat_card(
                "Inversion en compras",
                format_money(purchase_amount),
                f"{len(spend_items)} productos abastecidos",
                "negative",
            ),
            analysis_stat_card(
                "Producto top ventas",
                top_sale_item.get("label", "-") if top_sale_item else "-",
                (
                    f"{format_money(float(top_sale_item.get('amount') or 0.0))} · "
                    f"{format_compact_number(float(top_sale_item.get('qty') or 0.0))} uds"
                ) if top_sale_item else "Sin ventas",
                "positive",
            ),
            analysis_stat_card(
                "Mejor margen estimado",
                top_margin_item.get("label", "-") if top_margin_item else "-",
                format_signed_money(float(top_margin_item.get("margin") or 0.0)) if top_margin_item else "Sin datos cruzados",
                "positive" if top_margin_item and float(top_margin_item.get("margin") or 0.0) >= 0 else "negative",
            ),
        ]

        user_rows = sorted(
            [
                {
                    "label": row["label"],
                    "meta": (
                        f"{row['invoice_count']} facturas · "
                        f"{row['purchase_count']} compras · "
                        f"{row['expense_count']} gastos"
                    ),
                    "net_amount": float(row["invoice_amount"]) - float(row["purchase_amount"]) - float(row["expense_amount"]),
                    "raw_value": abs(float(row["invoice_amount"])) + abs(float(row["purchase_amount"])) + abs(float(row["expense_amount"])),
                }
                for row in user_activity.values()
            ],
            key=lambda item: item["raw_value"],
            reverse=True,
        )
        max_user_value = max([item["raw_value"] for item in user_rows] + [1.0])
        for item in user_rows:
            item["value"] = format_signed_money(item["net_amount"])
            item["pct"] = analysis_percent(item["raw_value"], max_user_value)
            item["tone"] = "profit" if item["net_amount"] >= 0 else "loss"

        client_rows = analysis_ranking_rows(
            sorted(client_totals.values(), key=lambda row: (row["amount"], row["count"]), reverse=True),
            value_key="amount",
            value_formatter=format_money,
            meta_builder=lambda item: f"{item['count']} facturas",
            limit=6,
        )
        provider_rows = analysis_ranking_rows(
            sorted(provider_totals.values(), key=lambda row: (row["amount"], row["count"]), reverse=True),
            value_key="amount",
            value_formatter=format_money,
            meta_builder=lambda item: f"{item['count']} compras",
            limit=6,
        )
        expense_rows = analysis_ranking_rows(
            sorted(expense_by_category.values(), key=lambda row: (row["amount"], row["count"]), reverse=True),
            value_key="amount",
            value_formatter=format_money,
            meta_builder=lambda item: f"{item['count']} movimientos",
            limit=6,
        )
        top_user_item = user_rows[0] if user_rows else None
        health_label = "Balance positivo" if net_amount >= 0 else "Balance tensionado"
        cash_direction_label = "Caja en crecimiento" if net_amount >= 0 else "Caja en retroceso"

        focus_cards_by_tab = {
            "resumen": [
                {
                    "label": "Margen bruto",
                    "value": format_signed_money(gross_margin_amount),
                    "note": "Facturacion menos compras",
                    "tone": "positive" if gross_margin_amount >= 0 else "negative",
                },
                {
                    "label": "Peso de compras",
                    "value": format_percent(analysis_ratio(purchase_amount, invoice_amount)),
                    "note": "Sobre ingresos facturados",
                    "tone": "negative",
                },
                {
                    "label": "Peso de gastos libres",
                    "value": format_percent(analysis_ratio(expense_amount, invoice_amount)),
                    "note": "Salida no ligada a producto",
                    "tone": "negative",
                },
            ],
            "productos": [
                {
                    "label": "Margen estimado",
                    "value": format_signed_money(gross_margin_amount),
                    "note": "Ventas del periodo frente a compras registradas",
                    "tone": "positive" if gross_margin_amount >= 0 else "negative",
                },
                {
                    "label": "Rotacion",
                    "value": (
                        f"{format_compact_number(sold_units / purchased_units)}x"
                        if purchased_units > 0 else "Sin base"
                    ),
                    "note": f"{sold_units:.0f} uds vendidas · {purchased_units:.0f} compradas",
                    "tone": "positive",
                },
                {
                    "label": "Stock bajo",
                    "value": str(len(current_low_stock)),
                    "note": "Productos por debajo del minimo",
                    "tone": "negative" if current_low_stock else "positive",
                },
            ],
            "operativa": [
                {
                    "label": "Usuario principal",
                    "value": top_user_item.get("label", "-") if top_user_item else "-",
                    "note": top_user_item.get("meta", "Sin actividad") if top_user_item else "Sin actividad",
                    "tone": "positive",
                },
                {
                    "label": "Cliente principal",
                    "value": top_client_item.get("label", "-") if top_client_item else "-",
                    "note": format_money(float(top_client_item.get("amount") or 0.0)) if top_client_item else "Sin clientes",
                    "tone": "positive",
                },
                {
                    "label": "Proveedor principal",
                    "value": top_provider_item.get("label", "-") if top_provider_item else "-",
                    "note": format_money(float(top_provider_item.get("amount") or 0.0)) if top_provider_item else "Sin proveedores",
                    "tone": "negative",
                },
            ],
        }

        return {
            "analysis_tabs": tabs,
            "analysis_presets": presets,
            "analysis_filters": {
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "tab": current_tab,
            },
            "analysis_header": {
                "range_label": range_label,
                "previous_label": previous_label,
                "period_days": period_days,
                "cash_balance": get_cashbox_state(config.CSV_CAJA).get("saldo_actual", "0.00"),
                "headline": "Vision global de la caja, consumo y operativa",
                "health_label": health_label,
                "cash_direction_label": cash_direction_label,
                "subline": (
                    "Combina ingresos por facturas, compras, gastos libres, productos y actividad "
                    "de administracion en un solo panel."
                ),
            },
            "analysis_focus_cards": focus_cards_by_tab.get(current_tab, focus_cards_by_tab["resumen"]),
            "overview_stats": overview_stats,
            "product_stats": product_stats,
            "operation_stats": [
                analysis_stat_card(
                    "Usuarios activos",
                    str(len(user_rows)),
                    (
                        f"{format_compact_number(total_movements / len(user_rows))} movimientos por usuario"
                        if user_rows else "Sin actividad registrada"
                    ),
                ),
                analysis_stat_card(
                    "Clientes activos",
                    str(client_count),
                    (
                        f"Principal {top_client_item.get('label', '-')}"
                        if top_client_item else f"{invoice_count} facturas emitidas"
                    ),
                    "positive",
                ),
                analysis_stat_card(
                    "Proveedores activos",
                    str(provider_count),
                    (
                        f"Principal {top_provider_item.get('label', '-')}"
                        if top_provider_item else f"{purchase_count} compras registradas"
                    ),
                    "negative",
                ),
                analysis_stat_card("Ticket medio", format_money(average_ticket), "Solo facturas del periodo"),
            ],
            "flow_rows": flow_rows[-8:],
            "flow_chart": {
                "points": flow_chart_points,
                "baseline": round(flow_chart_baseline, 2),
            },
            "flow_balance": {
                "incoming": format_money(invoice_amount),
                "outgoing": format_money(purchase_amount + expense_amount),
                "net": format_signed_money(net_amount),
            },
            "outflow_chart": {
                "style": outflow_style,
                "legend": outflow_legend,
            },
            "sales_rows": analysis_ranking_rows(
                sales_items,
                value_key="amount",
                value_formatter=format_money,
                meta_builder=lambda item: f"{format_compact_number(float(item['qty']))} uds · {item['lines']} lineas",
                limit=8,
            ),
            "product_margin_rows": product_margin_rows,
            "category_rows": category_rows,
            "user_rows": user_rows[:8],
            "client_rows": client_rows,
            "provider_rows": provider_rows,
            "expense_rows": expense_rows,
            "current_low_stock": current_low_stock,
            "analysis_empty": not (invoices or purchases or expenses),
        }

    @app.context_processor
    def inject_shell_context():
        nav_items = []
        if session.get("user"):
            settings_href = url_for("configuracion_caja") if getattr(g, "is_admin", False) else url_for("gestion_usuarios")
            settings_label = "Configuracion" if getattr(g, "is_admin", False) else "Usuario"
            nav_items = [
                {
                    "label": "Inicio",
                    "href": url_for("home"),
                    "endpoints": {"home"},
                },
                {
                    "label": "Inventario",
                    "href": url_for("inventario"),
                    "endpoints": {"inventario"},
                },
                {
                    "label": "Facturas",
                    "href": url_for("nueva_factura"),
                    "endpoints": {"nueva_factura", "nueva_factura_post", "historial_facturas", "imprimir_factura"},
                },
            ]
            if getattr(g, "is_admin", False):
                nav_items.extend([
                    {
                        "label": "Compras",
                        "href": url_for("nueva_compra"),
                        "endpoints": {"nueva_compra", "nueva_compra_post", "historial_compras"},
                    },
                    {
                        "label": "Gastos",
                        "href": url_for("nuevo_gasto_libre"),
                        "endpoints": {
                            "nuevo_gasto_libre",
                            "nuevo_gasto_libre_post",
                            "historial_gastos_libres",
                            "export_historial_gastos_libres",
                        },
                    },
                ])
            nav_items.append(
                {
                    "label": settings_label,
                    "href": settings_href,
                    "endpoints": (
                        {
                            "gestion_usuarios",
                            "cambiar_password",
                            "crear_usuario",
                            "eliminar_usuario",
                            "configuracion",
                            "configuracion_caja",
                            "configuracion_usuarios",
                            "configuracion_grupos",
                            "configuracion_migracion",
                            "configuracion_pantalla",
                            "configuracion_logs",
                            "actualizar_saldo_caja",
                            "actualizar_zoom_app",
                            "crear_grupo_config",
                            "editar_grupo_config",
                            "reasignar_grupo_config",
                            "eliminar_grupo_config",
                            "ejecutar_migracion_legacy",
                            "supervision",
                        }
                        if getattr(g, "is_admin", False)
                        else {"gestion_usuarios", "cambiar_password"}
                    ),
                }
            )

        return {
            "shell_nav_items": nav_items,
            "today_label": date.today().strftime("%d/%m/%Y"),
            "low_resource_mode": config.LOW_RESOURCE_MODE,
            "app_zoom_percent": get_app_zoom_percent(config.CSV_APP_SETTINGS),
            "app_zoom_scale": get_app_zoom_percent(config.CSV_APP_SETTINGS) / 100.0,
        }

    def print_invoice_ticket(factura_id: str) -> None:
        invoice = find_invoice(config.CSV_FACTURAS, factura_id)
        if not invoice:
            raise ValueError("Factura no encontrada.")

        lines = list_invoice_lines_for(config.CSV_FACTURA_LINEAS, factura_id)
        if not lines:
            raise ValueError("La factura no tiene lineas para imprimir.")

        ticket_text = format_invoice_ticket(invoice, lines)
        print_text_ticket(ticket_text, config.PRINT_JOBS_DIR)

    def print_shopping_list_ticket(items: list[dict[str, str]]) -> None:
        if not items:
            raise ValueError("La lista de compra no tiene productos para imprimir.")
        ticket_text = format_shopping_list_ticket(items)
        print_text_ticket(ticket_text, config.PRINT_JOBS_DIR)

    @app.after_request
    def audit_every_request(response):
        endpoint = (request.endpoint or "").strip()
        if endpoint == "static" or request.path.startswith("/static/"):
            return response
        if request.method == "GET" and response.status_code < 400 and not config.AUDIT_READ_REQUESTS:
            return response

        user = (getattr(g, "audit_user", "") or session.get("user", "") or "anon").strip()
        action = f"{request.method} {request.path}"
        detail = f"endpoint={endpoint or '-'} status={response.status_code}"
        log_action(config.CSV_LOGS, user, action, detail)
        return response

    # -------- AUTH --------
    @app.get("/login")
    def login():
        if session.get("user"):
            return redirect(url_for("home"))
        return render_template("login.html", title="Acceso")

    @app.post("/login")
    def login_post():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        g.audit_user = username or "anon"

        if verify_login(config.CSV_USUARIOS, username, password):
            session["user"] = username
            return redirect(url_for("home"))

        flash("Usuario o contraseña incorrectos.")
        return redirect(url_for("login"))

    @app.get("/logout")
    def logout():
        g.audit_user = session.get("user", "") or "anon"
        launched_by_launcher = session.get("launched_by_launcher")
        session.clear()
        if launched_by_launcher == "1":
            session["launched_by_launcher"] = "1"
        return redirect(url_for("login"))

    @app.get("/usuarios/gestion")
    def gestion_usuarios():
        r = require_login()
        if r:
            return r
        if user_is_admin():
            return redirect_to_config("usuarios")
        users = list_users(config.CSV_USUARIOS) if user_is_admin() else []
        user_stats = {
            "total": len(users),
            "admins": sum(1 for row in users if (row.get("rol") or "").strip().lower() == "admin"),
            "normal": sum(1 for row in users if (row.get("rol") or "normal").strip().lower() != "admin"),
        }
        return render_template(
            "usuarios_gestion.html",
            title="Gestion de usuario",
            is_admin=user_is_admin(),
            users=users,
            user_stats=user_stats,
        )

    @app.post("/usuarios/cambiar_password")
    def cambiar_password():
        r = require_login()
        if r:
            return r

        username = session.get("user", "")
        current_password = (request.form.get("password_actual") or "").strip()
        new_password = (request.form.get("password_nueva") or "").strip()
        repeat_password = (request.form.get("password_repetir") or "").strip()

        if not verify_login(config.CSV_USUARIOS, username, current_password):
            flash("La contraseña actual no es correcta.")
            return redirect_to_user_area()
        if new_password != repeat_password:
            flash("La nueva contraseña y la repetición no coinciden.")
            return redirect_to_user_area()

        update_password(
            config.CSV_USUARIOS,
            backup_dir=config.BACKUP_DIR,
            username=username,
            new_password=new_password,
        )
        flash("Contraseña actualizada ✅")
        return redirect_to_user_area()

    @app.post("/usuarios/crear")
    def crear_usuario():
        r = require_admin()
        if r:
            return r

        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        role = (request.form.get("rol") or "normal").strip().lower()

        if not username:
            flash("El nombre de usuario es obligatorio.")
            return redirect_to_config("usuarios")

        try:
            create_user(config.CSV_USUARIOS, username=username, password=password, rol=role)
        except ValueError as exc:
            flash(str(exc))
            return redirect_to_config("usuarios")

        flash("Usuario creado ✅")
        return redirect_to_config("usuarios")

    @app.post("/usuarios/eliminar")
    def eliminar_usuario():
        r = require_admin()
        if r:
            return r

        username = (request.form.get("username") or "").strip()
        if not username:
            flash("Usuario no válido.")
            return redirect_to_config("usuarios")

        me = (session.get("user") or "").strip().lower()
        if username.lower() == me:
            flash("No puedes desactivar tu propio usuario.")
            return redirect_to_config("usuarios")

        users = list_users(config.CSV_USUARIOS)
        target = next((u for u in users if (u.get("username") or "").strip().lower() == username.lower()), None)
        if not target:
            flash("Usuario no encontrado.")
            return redirect_to_config("usuarios")

        try:
            delete_user(config.CSV_USUARIOS, backup_dir=config.BACKUP_DIR, username=username)
        except ValueError as exc:
            flash(str(exc))
            return redirect_to_config("usuarios")

        flash("Usuario eliminado 🗑️")
        return redirect_to_config("usuarios")

    @app.get("/configuracion")
    def configuracion():
        return redirect(url_for("configuracion_caja"))

    @app.get("/configuracion/caja")
    def configuracion_caja():
        r = require_admin()
        if r:
            return r

        return render_template(
            "configuracion.html",
            title="Saldo de caja",
            config_section="caja",
            config_title="Configurar saldo de caja",
            config_subtitle="Las facturas suman y las compras o gastos libres restan automaticamente.",
            config_nav=config_shortcuts("caja"),
            cash_state=get_cashbox_state(config.CSV_CAJA),
        )

    @app.get("/configuracion/usuarios")
    def configuracion_usuarios():
        r = require_admin()
        if r:
            return r

        return render_template(
            "configuracion.html",
            title="Usuarios",
            config_section="usuarios",
            config_title="Configurar usuarios",
            config_subtitle="Gestiona accesos, roles y tu propia contraseña de administrador.",
            config_nav=config_shortcuts("usuarios"),
            **admin_user_context(),
        )

    @app.get("/configuracion/grupos")
    def configuracion_grupos():
        r = require_admin()
        if r:
            return r

        return render_template(
            "configuracion.html",
            title="Grupos de productos",
            config_section="grupos",
            config_title="Gestionar grupos de productos",
            config_subtitle="Crea nuevos grupos, reasigna productos y elimina grupos legacy que ya no uses.",
            config_nav=config_shortcuts("grupos"),
            **admin_groups_context(),
        )

    @app.post("/configuracion/grupos/crear")
    def crear_grupo_config():
        r = require_admin()
        if r:
            return r

        group_name = (request.form.get("nombre") or "").strip()
        emoji = (request.form.get("emoji") or "").strip()
        if not group_name:
            flash("Debes indicar el nombre del grupo.")
            return redirect_to_config("grupos")

        create_group(
            config.CSV_GRUPOS,
            group_name=group_name,
            emoji=emoji or "package",
            backup_dir=config.BACKUP_DIR,
        )
        flash("Grupo guardado ✅")
        return redirect_to_config("grupos")

    @app.post("/configuracion/grupos/editar")
    def editar_grupo_config():
        r = require_admin()
        if r:
            return r

        group_id = (request.form.get("group_id") or "").strip()
        group_name = (request.form.get("nombre") or "").strip()
        emoji = (request.form.get("emoji") or "").strip()
        if not group_id or not group_name:
            flash("Debes indicar un grupo valido.")
            return redirect_to_config("grupos")

        current_group = find_group_by_id(config.CSV_GRUPOS, group_id)
        if not current_group:
            flash("El grupo que intentas editar ya no existe.")
            return redirect_to_config("grupos")

        previous_name = (current_group.get("nombre") or "Otros").strip() or "Otros"
        updated_group = update_group(
            config.CSV_GRUPOS,
            group_id=group_id,
            group_name=group_name,
            emoji=emoji or "package",
            backup_dir=config.BACKUP_DIR,
        )
        if not updated_group:
            flash("No se pudo actualizar el grupo. Revisa si ya existe otro con ese nombre.")
            return redirect_to_config("grupos")

        new_name = (updated_group.get("nombre") or "Otros").strip() or "Otros"
        new_emoji = (updated_group.get("emoji") or "package").strip() or "package"
        new_emoji_char = str(get_emoji_entry(new_emoji)["char"])
        product_updates: dict[str, dict[str, str]] = {}
        for product in get_products(config.CSV_PRODUCTOS):
            current_name = (product.get("grupo") or "Otros").strip() or "Otros"
            if current_name != previous_name:
                continue
            updates = {"grupo_emoji": new_emoji_char}
            if current_name != new_name:
                updates["grupo"] = new_name
            product_updates[product["producto_id"]] = updates

        if product_updates:
            update_many_product_fields(
                config.CSV_PRODUCTOS,
                backup_dir=config.BACKUP_DIR,
                updates=product_updates,
            )

        flash("Grupo actualizado ✅")
        return redirect_to_config("grupos")

    @app.post("/configuracion/grupos/reasignar")
    def reasignar_grupo_config():
        r = require_admin()
        if r:
            return r

        source_group = (request.form.get("grupo_origen") or "").strip()
        target_group = (request.form.get("grupo_destino") or "").strip()
        if not source_group or not target_group:
            flash("Debes indicar grupo origen y grupo destino.")
            return redirect_to_config("grupos")
        if source_group == target_group:
            flash("El grupo origen y el destino no pueden ser el mismo.")
            return redirect_to_config("grupos")

        target_group_row = find_group_by_name(config.CSV_GRUPOS, target_group)
        if not target_group_row:
            flash("El grupo destino no existe.")
            return redirect_to_config("grupos")

        products = get_products(config.CSV_PRODUCTOS)
        product_updates: dict[str, dict[str, str]] = {}
        changed = 0
        for product in products:
            current_group = (product.get("grupo") or "Otros").strip() or "Otros"
            if current_group != source_group:
                continue
            changed += 1
            product_updates[product["producto_id"]] = {
                "grupo": target_group,
                "grupo_emoji": str(get_emoji_entry((target_group_row.get("emoji") or "package").strip() or "package")["char"]),
            }

        if not product_updates:
            flash("No hay productos en el grupo origen.")
            return redirect_to_config("grupos")

        update_many_product_fields(
            config.CSV_PRODUCTOS,
            backup_dir=config.BACKUP_DIR,
            updates=product_updates,
        )
        flash(f"Productos reasignados: {changed} ✅")
        return redirect_to_config("grupos")

    @app.post("/configuracion/grupos/eliminar")
    def eliminar_grupo_config():
        r = require_admin()
        if r:
            return r

        group_name = (request.form.get("grupo") or "").strip()
        if not group_name:
            flash("Grupo no válido.")
            return redirect_to_config("grupos")

        product_updates: dict[str, dict[str, str]] = {}
        for product in get_products(config.CSV_PRODUCTOS):
            if stored_group_name(product.get("grupo")) != group_name:
                continue
            product_updates[product["producto_id"]] = {
                "grupo": "",
                "grupo_emoji": str(get_emoji_entry("question")["char"]),
            }

        try:
            if product_updates:
                update_many_product_fields(
                    config.CSV_PRODUCTOS,
                    backup_dir=config.BACKUP_DIR,
                    updates=product_updates,
                )
        except Exception as exc:
            print(
                f"[BuenYantar] Aviso: no se pudieron dejar sin grupo algunos productos al eliminar grupo '{group_name}': {exc}",
                file=sys.stderr,
            )

        try:
            delete_group(config.CSV_GRUPOS, group_name=group_name, backup_dir=config.BACKUP_DIR)
        except Exception as exc:
            print(
                f"[BuenYantar] Aviso: no se pudo marcar como inactivo el grupo '{group_name}': {exc}",
                file=sys.stderr,
            )

        if product_updates:
            flash(f"Grupo eliminado 🗑️ Productos sin grupo: {len(product_updates)}")
        else:
            flash("Grupo eliminado 🗑️")
        return redirect_to_config("grupos")

    @app.get("/configuracion/migracion")
    def configuracion_migracion():
        r = require_admin()
        if r:
            return r

        return render_template(
            "configuracion.html",
            title="Migracion legacy",
            config_section="migracion",
            config_title="Migracion desde archivos legacy",
            config_subtitle="Sustituye usuarios, inventario y facturas por la version antigua, y fija la caja con saldo inicial anual y saldo actual.",
            config_nav=config_shortcuts("migracion"),
        )

    @app.get("/configuracion/pantalla")
    def configuracion_pantalla():
        r = require_admin()
        if r:
            return r

        current_zoom = get_app_zoom_percent(config.CSV_APP_SETTINGS)
        return render_template(
            "configuracion.html",
            title="Pantalla y zoom",
            config_section="pantalla",
            config_title="Pantalla y zoom",
            config_subtitle="Fija el zoom con el que la aplicación se abrirá por defecto en este equipo.",
            config_nav=config_shortcuts("pantalla"),
            app_zoom_percent=current_zoom,
            app_zoom_options=[80, 90, 100, 110, 125, 150],
        )

    @app.post("/configuracion/pantalla")
    def actualizar_zoom_app():
        r = require_admin()
        if r:
            return r

        raw_zoom = (request.form.get("app_zoom_percent") or "").strip()
        try:
            zoom_percent = int(raw_zoom)
        except ValueError:
            flash("El zoom indicado no es válido.")
            return redirect_to_config("pantalla")

        if zoom_percent < 50 or zoom_percent > 200:
            flash("El zoom debe estar entre 50% y 200%.")
            return redirect_to_config("pantalla")

        set_app_setting(
            config.CSV_APP_SETTINGS,
            key="app_zoom_percent",
            value=str(zoom_percent),
            updated_by=session.get("user", ""),
            backup_dir=config.BACKUP_DIR,
        )
        flash("Zoom de apertura actualizado ✅")
        return redirect_to_config("pantalla")

    @app.post("/configuracion/migracion")
    def ejecutar_migracion_legacy():
        r = require_admin()
        if r:
            return r

        folder_raw = (request.form.get("legacy_folder") or "").strip()
        cash_raw = (request.form.get("saldo_caja_actual") or request.form.get("saldo_caja_final") or "").strip()
        year_start_cash_raw = (request.form.get("saldo_caja_inicio_anio") or "").strip()
        if not folder_raw:
            flash("Debes indicar la carpeta que contiene los archivos legacy.")
            return redirect_to_config("migracion")

        legacy_folder = Path(folder_raw).expanduser()
        if not legacy_folder.is_absolute():
            legacy_folder = (config.BASE_DIR / legacy_folder).resolve()

        try:
            final_cash = round(parse_decimal(cash_raw), 2)
        except ValueError:
            flash("El saldo actual de caja no es válido.")
            return redirect_to_config("migracion")

        try:
            year_start_cash = round(parse_decimal(year_start_cash_raw), 2)
        except ValueError:
            flash("El saldo al inicio del año no es válido.")
            return redirect_to_config("migracion")

        try:
            result = migrate_legacy_dataset(
                legacy_folder=legacy_folder,
                saldo_final_caja=final_cash,
                saldo_inicio_anio=year_start_cash,
                csv_usuarios=config.CSV_USUARIOS,
                csv_productos=config.CSV_PRODUCTOS,
                csv_grupos=config.CSV_GRUPOS,
                csv_movs=config.CSV_MOVS,
                csv_facturas=config.CSV_FACTURAS,
                csv_factura_lineas=config.CSV_FACTURA_LINEAS,
                csv_caja=config.CSV_CAJA,
                csv_logs=config.CSV_LOGS,
                csv_gastos=config.CSV_GASTOS_LIBRES,
                backup_dir=config.BACKUP_DIR,
                imported_by=session.get("user", ""),
            )
            refresh_cash_analysis_storage(force=True)
            log_action(
                config.CSV_LOGS,
                session.get("user", ""),
                "MIGRACION LEGACY",
                (
                    f"Carpeta={legacy_folder} | usuarios={result.users} | productos={result.products} | "
                    f"grupos={result.groups} | facturas={result.invoices} | lineas={result.invoice_lines} | "
                    f"ajustes_stock={result.stock_adjustments} | placeholders={result.placeholders} | "
                    f"saldo_inicio_anio={year_start_cash:.2f} | saldo_actual={final_cash:.2f}"
                ),
            )
        except Exception as exc:
            flash(f"No se pudo completar la migración: {exc}")
            return redirect_to_config("migracion")

        flash(
            "Migración completada ✅ "
            f"Usuarios: {result.users}, productos: {result.products}, grupos: {result.groups}, "
            f"facturas: {result.invoices}, líneas: {result.invoice_lines}."
        )
        return redirect_to_config("migracion")

    @app.get("/configuracion/logs")
    def configuracion_logs():
        r = require_admin()
        if r:
            return r

        return render_template(
            "configuracion.html",
            title="Logs de uso",
            config_section="logs",
            config_title="Revisar logs de uso",
            config_subtitle="Filtra actividad de la aplicacion, usuarios y respuestas HTTP.",
            config_nav=config_shortcuts("logs"),
            **admin_logs_context(),
        )

    @app.post("/configuracion/caja")
    def actualizar_saldo_caja():
        r = require_admin()
        if r:
            return r

        saldo_raw = (request.form.get("saldo_caja") or "").strip()
        note = (request.form.get("nota") or "").strip()
        try:
            new_balance = round(parse_decimal(saldo_raw), 2)
        except ValueError:
            flash("Saldo de caja no válido.")
            return redirect_to_config("caja")

        previous_balance = get_cash_balance(config.CSV_CAJA)
        set_cash_balance(
            config.CSV_CAJA,
            amount=new_balance,
            updated_by=session.get("user", ""),
            note=note,
        )
        log_action(
            config.CSV_LOGS,
            session.get("user", ""),
            "AJUSTE CAJA",
            f"Saldo {previous_balance:.2f} -> {new_balance:.2f}. Nota: {note or '-'}",
        )
        flash("Saldo de caja actualizado ✅")
        return redirect_to_config("caja")

    @app.get("/")
    def root():
        return redirect(url_for("home"))

    @app.post("/launcher/exit")
    def launcher_exit():
        r = require_login_json()
        if r:
            return r

        signal_path = launcher_exit_signal_path()
        if signal_path is None:
            return jsonify({
                "ok": False,
                "error": "Esta ventana no fue abierta por el lanzador. Puedes cerrarla manualmente.",
            }), 409

        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text("close", encoding="utf-8")
        return jsonify({"ok": True})

    @app.get("/home")
    def home():
        r = require_login()
        if r:
            return r

        is_admin_user = user_is_admin()
        products_all = get_products(config.CSV_PRODUCTOS)
        stock_map = calc_stock_by_product(config.CSV_MOVS)
        snapshot = inventory_snapshot(products_all, stock_map)
        products = [
            p for p in products_all
            if p.get("stock_infinito", "0") != "1"
        ]
        attention_products = []
        shopping_products = []
        for p in products:
            pid = p.get("producto_id", "")
            current_stock = stock_map.get(pid, 0.0)
            stock_min = safe_float(p.get("stock_minimo"))
            is_low_stock = current_stock < stock_min
            product_summary = {
                "producto_id": pid,
                "nombre": p.get("nombre", ""),
                "grupo": display_group_name(p.get("grupo")),
                "grupo_emoji": display_group_emoji(p.get("grupo_emoji")),
                "stock_label": format_compact_number(current_stock),
                "stock_minimo_label": format_compact_number(stock_min),
                "low_stock": is_low_stock,
                "href": url_for("inventario", producto_id=pid, low="1" if is_low_stock else "0"),
            }
            shopping_products.append(product_summary)
            if not is_low_stock:
                continue
            attention_products.append(product_summary)
        attention_products.sort(
            key=lambda row: (
                -(safe_float(row["stock_minimo_label"]) - safe_float(row["stock_label"])),
                row["nombre"].lower(),
            )
        )
        shopping_products.sort(
            key=lambda row: (
                0 if row["low_stock"] else 1,
                -(safe_float(row["stock_minimo_label"]) - safe_float(row["stock_label"])) if row["low_stock"] else 0,
                row["nombre"].lower(),
            )
        )

        invoices = list_invoices(config.CSV_FACTURAS, limit=6)
        if not is_admin_user:
            me = (session.get("user") or "").strip().lower()
            invoices = [
                row for row in invoices
                if (row.get("usuario") or "").strip().lower() == me
            ]

        recent_activity = []
        for inv in invoices:
            amount = safe_float(inv.get("total_importe"))
            recent_activity.append({
                "icon": "🧾",
                "label": "Factura",
                "user": (inv.get("usuario") or "").strip() or "-",
                "date": (inv.get("fecha") or "")[:10],
                "cash_delta": f"+{amount:.2f} €",
                "cash_delta_class": "home-compact-delta-positive",
                "sort_key": inv.get("fecha", ""),
                "href": url_for("historial_facturas", q=inv.get("factura_id", "")),
            })
        if is_admin_user:
            product_names = {
                p.get("producto_id", ""): p.get("nombre", "")
                for p in products_all
            }
            tickets, _ = list_purchase_history(
                config.CSV_MOVS,
                product_names=product_names,
                limit=6,
            )
            free_expenses = list_free_expenses(config.CSV_GASTOS_LIBRES, limit=6)
            for ticket in tickets:
                amount = safe_float(ticket.get("total_importe"))
                recent_activity.append({
                    "icon": "🛒",
                    "label": "Compra",
                    "user": (ticket.get("usuario") or "").strip() or "-",
                    "date": (ticket.get("fecha") or "")[:10],
                    "cash_delta": f"-{amount:.2f} €",
                    "cash_delta_class": "home-compact-delta-negative",
                    "sort_key": ticket.get("fecha", ""),
                    "href": url_for("historial_compras", q=ticket.get("ref_id", "")),
                })
            for expense in free_expenses:
                amount = safe_float(expense.get("importe"))
                recent_activity.append({
                    "icon": "💸",
                    "label": "Gasto",
                    "user": (expense.get("usuario") or "").strip() or "-",
                    "date": (expense.get("fecha") or "")[:10],
                    "cash_delta": f"-{amount:.2f} €",
                    "cash_delta_class": "home-compact-delta-negative",
                    "sort_key": expense.get("fecha", ""),
                    "href": url_for("historial_gastos_libres", q=expense.get("gasto_id", "")),
                })
        recent_activity.sort(key=lambda row: row.get("sort_key", ""), reverse=True)

        return render_template(
            "home.html",
            title="Pantalla principal",
            home_stats=snapshot,
            cash_state=get_cashbox_state(config.CSV_CAJA) if is_admin_user else None,
            recent_activity=recent_activity[:6],
            attention_products=attention_products,
            shopping_products=shopping_products,
        )

    @app.get("/analisis")
    def analisis():
        r = require_admin()
        if r:
            return r

        refresh_cash_analysis_storage()

        default_from, default_to = analysis_detail_range_defaults()
        start_date = parse_query_date(request.args.get("from", ""), default_from)
        end_date = parse_query_date(request.args.get("to", ""), default_to)
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        annual_year_options = analysis_available_years(config.CSV_CAJA_MOVIMIENTOS)
        current_year = date.today().year
        selected_annual_year = parse_analysis_year(
            request.args.get("year", ""),
            current_year,
            annual_year_options,
        )
        current_view = (request.args.get("view") or "detalle").strip().lower()
        if current_view not in {"detalle", "resumen-anual"}:
            current_view = "detalle"
        selected_detail_types = normalize_analysis_detail_types(request.args.getlist("tipo"))

        detail_rows_all = list_cash_detail_rows(
            config.CSV_CAJA_MOVIMIENTOS,
            start_date=start_date,
            end_date=end_date,
        )
        detail_rows = [
            row for row in detail_rows_all
            if not selected_detail_types or row.get("tipo") in selected_detail_types
        ]
        detail_summary = cash_detail_summary(
            config.CSV_CAJA_MOVIMIENTOS,
            start_date=start_date,
            end_date=end_date,
        )
        detail_preview_rows = detail_rows[:200]
        detail_table_total = round(sum(safe_float(row.get("importe")) for row in detail_rows), 2)
        annual_summary = build_analysis_annual_summary(selected_annual_year)

        return render_template(
            "analisis.html",
            title="Analisis",
            analysis_view=current_view,
            analysis_views=[
                {
                    "key": "detalle",
                    "label": "TABLA DE DETALLE",
                    "description": "Exporta un Excel con el detalle filtrado del saldo de caja.",
                    "href": url_for("analisis", **analysis_detail_query_params(start_date, end_date, selected_detail_types, view="detalle")),
                    "active": current_view == "detalle",
                },
                {
                    "key": "resumen-anual",
                    "label": "RESUMEN ANUAL",
                    "description": "Agrupa la caja por meses y resume el saldo del periodo.",
                    "href": url_for("analisis", view="resumen-anual", year=selected_annual_year),
                    "active": current_view == "resumen-anual",
                },
            ],
            detail_filters={
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "tipos": selected_detail_types,
            },
            detail_type_options=analysis_detail_type_options(),
            detail_rows=detail_preview_rows,
            detail_rows_total=len(detail_rows),
            detail_preview_limited=len(detail_rows) > len(detail_preview_rows),
            detail_table_total=detail_table_total,
            detail_summary=detail_summary,
            detail_export_href=url_for("export_analisis_detalle", **analysis_detail_query_params(start_date, end_date, selected_detail_types)),
            annual_filters={
                "year": selected_annual_year,
            },
            annual_year_options=annual_year_options,
            annual_summary=annual_summary,
            annual_export_href=url_for("export_analisis_resumen_anual", year=selected_annual_year),
        )

    @app.get("/analisis/resumen-anual/export")
    def export_analisis_resumen_anual():
        r = require_admin()
        if r:
            return r

        refresh_cash_analysis_storage()

        annual_year_options = analysis_available_years(config.CSV_CAJA_MOVIMIENTOS)
        current_year = date.today().year
        selected_annual_year = parse_analysis_year(
            request.args.get("year", ""),
            current_year,
            annual_year_options,
        )
        annual_summary = build_analysis_annual_summary(selected_annual_year)
        workbook = build_cash_annual_workbook(
            annual_summary["rows"],
            selected_year=selected_annual_year,
            opening_balance=float(annual_summary["opening_balance"]),
            closing_balance=float(annual_summary["closing_balance"]),
            net_balance=float(annual_summary["net_balance"]),
        )

        response = Response(
            workbook,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response.headers["Content-Disposition"] = (
            f'attachment; filename="resumen_anual_{selected_annual_year}.xlsx"'
        )
        return response

    @app.get("/analisis/detalle/export")
    def export_analisis_detalle():
        r = require_admin()
        if r:
            return r

        refresh_cash_analysis_storage()

        default_from, default_to = analysis_detail_range_defaults()
        start_date = parse_query_date(request.args.get("from", ""), default_from)
        end_date = parse_query_date(request.args.get("to", ""), default_to)
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        selected_detail_types = normalize_analysis_detail_types(request.args.getlist("tipo"))

        rows = list_cash_detail_rows(
            config.CSV_CAJA_MOVIMIENTOS,
            start_date=start_date,
            end_date=end_date,
        )
        if selected_detail_types:
            rows = [row for row in rows if row.get("tipo") in selected_detail_types]
        workbook = build_cash_detail_workbook(rows)

        response = Response(
            workbook,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response.headers["Content-Disposition"] = (
            f'attachment; filename="analisis_detalle_{start_date.strftime("%Y%m%d")}_{end_date.strftime("%Y%m%d")}.xlsx"'
        )
        return response

    @app.post("/listas/compra/imprimir")
    def imprimir_lista_compra():
        r = require_login_json()
        if r:
            return r

        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("product_ids") or []
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "La seleccion de productos no es valida."}), 400

        selected_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in raw_ids:
            pid = str(raw_id or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            selected_ids.append(pid)

        if not selected_ids:
            return jsonify({"ok": False, "error": "Selecciona al menos un producto."}), 400

        products = {
            p.get("producto_id", ""): p
            for p in get_products(config.CSV_PRODUCTOS)
            if p.get("stock_infinito", "0") != "1"
        }
        stock_map = calc_stock_by_product(config.CSV_MOVS)

        items: list[dict[str, str]] = []
        for pid in selected_ids:
            product = products.get(pid)
            if not product:
                continue
            current_stock = stock_map.get(pid, 0.0)
            stock_min = safe_float(product.get("stock_minimo"))
            items.append({
                "nombre": product.get("nombre", ""),
                "grupo_emoji": display_group_emoji(product.get("grupo_emoji")),
                "stock_actual": format_compact_number(current_stock),
                "stock_seguridad": format_compact_number(stock_min),
            })

        if not items:
            return jsonify({"ok": False, "error": "No hay productos validos para imprimir."}), 400

        try:
            print_shopping_list_ticket(items)
        except (ValueError, RuntimeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

        return jsonify({"ok": True, "message": "Lista enviada a la impresora conectada."})

    # -------- INVENTARIO --------
    @app.get("/inventario")
    def inventario():
        r = require_login()
        if r:
            return r

        producto_id = request.args.get("producto_id", "").strip()
        current_filters = {
            "q": (request.args.get("q") or "").strip(),
            "g": (request.args.get("g") or "").strip(),
            "low": "1" if (request.args.get("low") or "").strip() == "1" else "0",
        }
        selected_group_filter = normalize_group_filter_value(current_filters["g"])

        prods = get_products(config.CSV_PRODUCTOS)
        stock_map = calc_stock_by_product(config.CSV_MOVS)

        rows = []
        low_stock_count = 0
        infinite_stock_count = 0
        for p in prods:
            group_name = stored_group_name(p.get("grupo"))
            group_emoji = display_group_emoji(p.get("grupo_emoji"))

            pid = p["producto_id"]
            infinito = (p.get("stock_infinito", "0") == "1")
            stock = stock_map.get(pid, 0.0)
            stock_min = float(p.get("stock_minimo") or 0.0)
            bajo_min = (False if infinito else (stock < stock_min))
            stock_display_main, stock_display_meta = stock_display_parts(p, None if infinito else stock)

            if infinito:
                infinite_stock_count += 1
            elif bajo_min:
                low_stock_count += 1

            rows.append({
                "producto_id": pid,
                "nombre": p.get("nombre", ""),
                "unidad": stock_unit_label(p),
                "unidad_venta": sale_unit_label(p),
                "grupo": group_name,
                "grupo_label": display_group_name(group_name),
                "grupo_emoji": group_emoji,
                "stock_infinito": "1" if infinito else "0",
                "fraccionable": "1" if product_is_fractionable(p) else "0",
                "fracciones_por_unidad": format_compact_number(product_fraction_count(p)),
                "fraccion_info": fraction_info_label(p),
                "stock_minimo": str(p.get("stock_minimo", "0")),
                "total": None if infinito else stock,
                "stock_display_main": stock_display_main,
                "stock_display_meta": stock_display_meta,
                "bajo_minimo": "1" if bajo_min else "0",
                "state_label": "INF" if infinito else ("0" if stock == 0 else ("BAJO" if bajo_min else "OK")),
                "state_class": "state-inf" if infinito else ("state-zero" if stock == 0 else ("state-low" if bajo_min else "state-ok")),
            })
        rows.sort(key=lambda x: x["nombre"].lower())
        group_options = group_options_for_products(prods)

        if config.LOW_RESOURCE_MODE:
            query = normalize_text_search(current_filters["q"])
            group_name = current_filters["g"]
            low_only = current_filters["low"] == "1"
            filtered_rows = [
                row for row in rows
                if (not query or query in normalize_text_search(row["nombre"]))
                and (selected_group_filter is None or row["grupo"] == selected_group_filter)
                and (not low_only or row["bajo_minimo"] == "1")
            ]
            visible_rows, product_browser = paginate_rows(
                filtered_rows,
                per_page=24,
                endpoint="inventario",
                current_filters=current_filters,
            )
        else:
            visible_rows = rows
            product_browser = {
                "page": 1,
                "page_count": 1,
                "per_page": len(rows),
                "total": len(rows),
                "shown_from": 1 if rows else 0,
                "shown_to": len(rows),
                "has_prev": False,
                "has_next": False,
                "prev_url": "",
                "next_url": "",
                "current_url": url_for(
                    "inventario",
                    **{
                        key: value
                        for key, value in current_filters.items()
                        if value and not (key == "low" and value != "1")
                    },
                ),
            }

        if is_partial_json_request():
            payload_rows = []
            for row in visible_rows:
                payload_row = dict(row)
                payload_row["href"] = url_for(
                    "inventario",
                    producto_id=row.get("producto_id"),
                    q=current_filters["q"] or None,
                    g=current_filters["g"] or None,
                    low="1" if current_filters["low"] == "1" else None,
                    page=product_browser["page"] if config.LOW_RESOURCE_MODE and product_browser["page"] > 1 else None,
                )
                payload_rows.append(payload_row)
            return jsonify({
                "ok": True,
                "items": payload_rows,
                "browser": product_browser,
                "summary": product_browser_summary(
                    product_browser,
                    "No hay productos que coincidan con los filtros actuales.",
                ),
                "selected_producto_id": producto_id,
            })

        selected = find_product(config.CSV_PRODUCTOS, producto_id) if producto_id else None

        selected_stock = None
        selected_sale_stock = None
        selected_purchases = []
        purchase_recommendation = None
        if selected:
            sel_inf = (selected.get("stock_infinito", "0") == "1")
            selected_stock = None if sel_inf else stock_map.get(producto_id, 0.0)
            selected_sale_stock = None if selected_stock is None else stock_quantity_to_sale_quantity(selected, selected_stock)
            if user_is_admin():
                selected_purchases = last_purchases_for_product(
                    config.CSV_MOVS,
                    producto_id,
                    limit=page_limit(300, 80),
                )
                latest_purchase_price = 0.0
                latest_purchase_date = ""
                for movement in selected_purchases:
                    raw_price = extract_note_value(movement.get("nota", ""), "€/u:")
                    price_value = safe_float(raw_price)
                    if price_value > 0:
                        latest_purchase_price = price_value
                        latest_purchase_date = (movement.get("fecha") or "")[:10]
                        break
                if latest_purchase_price > 0:
                    recommendation_price = latest_purchase_price
                    recommendation_unit = stock_unit_label(selected)
                    purchase_reference = ""
                    if product_is_fractionable(selected):
                        recommendation_price = latest_purchase_price / product_fraction_count(selected)
                        recommendation_unit = sale_unit_label(selected)
                        purchase_reference = f"Ultima compra registrada: {latest_purchase_price:.2f} € / {stock_unit_label(selected)}"
                    purchase_recommendation = {
                        "ultima_compra": recommendation_price,
                        "recomendado": round(recommendation_price * 1.2, 2),
                        "fecha": latest_purchase_date,
                        "unidad": recommendation_unit,
                        "referencia_compra": purchase_reference,
                    }

        inventory_summary = {
            "total_products": len(rows),
            "low_stock": low_stock_count,
            "infinite_stock": infinite_stock_count,
            "groups": len(group_options),
        }

        return render_template(
            "inventario.html",
            title="Inventario",
            products_table=visible_rows,
            selected=selected,
            selected_stock=selected_stock,
            selected_sale_stock=selected_sale_stock,
            selected_purchases=selected_purchases,
            purchase_recommendation=purchase_recommendation,
            group_options=group_options,
            current_filters=current_filters,
            inventory_summary=inventory_summary,
            product_browser=product_browser,
        )

    @app.post("/inventario/nuevo_producto")
    def inventario_nuevo_producto():
        r = require_admin()
        if r:
            return r

        nombre = request.form.get("nombre", "").strip()
        precio_unitario = request.form.get("precio_unitario", "").strip()
        unidad = (request.form.get("unidad", "ud") or "ud").strip()
        stock_minimo = (request.form.get("stock_minimo", "0") or "0").strip()
        stock_actual = (request.form.get("stock_actual", "") or "").strip()
        stock_infinito = "1" if request.form.get("stock_infinito") == "on" else "0"
        fraccionable = "1" if request.form.get("fraccionable") == "on" else "0"
        fracciones_por_unidad_raw = (request.form.get("fracciones_por_unidad", "1") or "1").strip()
        unidad_venta_raw = (request.form.get("unidad_venta", "") or "").strip()

        grupo_select = (request.form.get("grupo_select") or "").strip()

        prods = get_products(config.CSV_PRODUCTOS)
        sync_group_catalog_safe(products=prods)
        try:
            groups_map = {
                stored_group_name(row.get("nombre")):
                str(get_emoji_entry((row.get("emoji") or "package").strip() or "package")["char"])
                for row in list_groups(config.CSV_GRUPOS)
            }
        except Exception as exc:
            print(
                f"[BuenYantar] Aviso: no se pudieron cargar los grupos al crear producto: {exc}",
                file=sys.stderr,
            )
            groups_map = {}

        if grupo_select == "__new__":
            grupo = (request.form.get("grupo_custom") or "").strip()
            grupo_emoji_value = (request.form.get("grupo_emoji_custom") or "").strip()
            if not grupo:
                flash("Si eliges 'Nuevo grupo', debes indicar el nombre del grupo.")
                return inventario_redirect_with_filters()
            if not grupo_emoji_value:
                flash("Si eliges 'Nuevo grupo', debes indicar el emoji del grupo.")
                return inventario_redirect_with_filters()
            grupo_emoji_key = get_emoji_key(grupo_emoji_value)
            grupo_emoji = str(get_emoji_entry(grupo_emoji_key)["char"])
        else:
            grupo = grupo_select
            grupo_emoji = groups_map.get(grupo, str(get_emoji_entry("question")["char"]) if not grupo else str(get_emoji_entry("package")["char"]))

        if not nombre:
            flash("El nombre del producto es obligatorio.")
            return inventario_redirect_with_filters()

        if fraccionable == "1":
            if not unidad_venta_raw:
                flash("Si el producto es fraccionable, debes indicar la unidad de venta.")
                return inventario_redirect_with_filters()
            try:
                fracciones_por_unidad = parse_decimal(fracciones_por_unidad_raw)
            except ValueError:
                flash("Fracciones por unidad no válido. Debe ser un número.")
                return inventario_redirect_with_filters()
            if fracciones_por_unidad <= 0:
                flash("Fracciones por unidad debe ser mayor que 0.")
                return inventario_redirect_with_filters()
            unidad_venta = unidad_venta_raw
            fracciones_por_unidad_clean = format_compact_number(fracciones_por_unidad)
        else:
            unidad_venta = unidad
            fracciones_por_unidad_clean = "1"

        stock_actual_value = None
        if stock_infinito == "0":
            if stock_actual == "":
                flash("Si el producto no es infinito, debes indicar el stock actual.")
                return inventario_redirect_with_filters()
            try:
                stock_actual_value = float(stock_actual.replace(",", "."))
            except ValueError:
                flash("Stock actual no válido. Debe ser un número.")
                return inventario_redirect_with_filters()

        pid = create_product(
            config.CSV_PRODUCTOS,
            nombre=nombre,
            precio_unitario=precio_unitario,
            unidad=unidad,
            stock_minimo=stock_minimo,
            grupo=grupo,
            grupo_emoji=grupo_emoji,
            stock_infinito=stock_infinito,
            fraccionable=fraccionable,
            fracciones_por_unidad=fracciones_por_unidad_clean,
            unidad_venta=unidad_venta,
        )
        if grupo_select == "__new__":
            create_group(
                config.CSV_GRUPOS,
                group_name=grupo,
                emoji=grupo_emoji_key,
                backup_dir=config.BACKUP_DIR,
            )
        if stock_actual_value is not None:
            set_stock_to_value(config.CSV_MOVS, pid, desired_stock=stock_actual_value)

        flash("Producto añadido ✅")
        return inventario_redirect_with_filters(producto_id=pid)

    @app.post("/inventario/editar")
    def inventario_editar():
        r = require_admin()
        if r:
            return r

        producto_id = request.form.get("producto_id", "").strip()
        if not producto_id:
            flash("Producto no válido.")
            return inventario_redirect_with_filters()

        nombre = request.form.get("nombre", "").strip()
        precio_unitario = request.form.get("precio_unitario", "").strip()
        unidad = request.form.get("unidad", "").strip() or "ud"
        stock_minimo = request.form.get("stock_minimo", "").strip() or "0"
        stock_actual_raw = request.form.get("stock_actual", "").strip()
        fraccionable = "1" if request.form.get("fraccionable") == "on" else "0"
        fracciones_por_unidad_raw = (request.form.get("fracciones_por_unidad", "1") or "1").strip()
        unidad_venta_raw = (request.form.get("unidad_venta", "") or "").strip()

        grupo_select = (request.form.get("grupo_select") or "").strip()
        legacy_grupo = (request.form.get("grupo") or "").strip()
        legacy_grupo_emoji = (request.form.get("grupo_emoji") or "").strip()

        prods = get_products(config.CSV_PRODUCTOS)
        sync_group_catalog_safe(products=prods)
        try:
            groups_map = {
                stored_group_name(row.get("nombre")):
                str(get_emoji_entry((row.get("emoji") or "package").strip() or "package")["char"])
                for row in list_groups(config.CSV_GRUPOS)
            }
        except Exception as exc:
            print(
                f"[BuenYantar] Aviso: no se pudieron cargar los grupos al editar producto: {exc}",
                file=sys.stderr,
            )
            groups_map = {}

        if grupo_select == "__new__":
            grupo = (request.form.get("grupo_custom") or "").strip()
            grupo_emoji_value = (request.form.get("grupo_emoji_custom") or "").strip()
            if not grupo:
                flash("Si eliges 'Nuevo grupo', debes indicar el nombre del grupo.")
                return inventario_redirect_with_filters(producto_id=producto_id)
            if not grupo_emoji_value:
                flash("Si eliges 'Nuevo grupo', debes indicar el emoji del grupo.")
                return inventario_redirect_with_filters(producto_id=producto_id)
            grupo_emoji_key = get_emoji_key(grupo_emoji_value)
            grupo_emoji = str(get_emoji_entry(grupo_emoji_key)["char"])
        else:
            grupo = grupo_select if grupo_select != "" else ""
            grupo_emoji = groups_map.get(
                grupo,
                str(get_emoji_entry("question")["char"]) if not grupo else str(get_emoji_entry(get_emoji_key(legacy_grupo_emoji or "package"))["char"]),
            )
            grupo_emoji_key = get_emoji_key(grupo_emoji)

        stock_infinito = "1" if request.form.get("stock_infinito") == "on" else "0"
        if fraccionable == "1":
            if not unidad_venta_raw:
                flash("Debes indicar la unidad de venta para un producto fraccionable.")
                return inventario_redirect_with_filters(producto_id=producto_id)
            try:
                fracciones_por_unidad = parse_decimal(fracciones_por_unidad_raw)
            except ValueError:
                flash("Fracciones por unidad no válido. Debe ser un número.")
                return inventario_redirect_with_filters(producto_id=producto_id)
            if fracciones_por_unidad <= 0:
                flash("Fracciones por unidad debe ser mayor que 0.")
                return inventario_redirect_with_filters(producto_id=producto_id)
            unidad_venta = unidad_venta_raw
            fracciones_por_unidad_clean = format_compact_number(fracciones_por_unidad)
        else:
            unidad_venta = unidad
            fracciones_por_unidad_clean = "1"
        if stock_infinito == "1":
            stock_minimo = "0"
            stock_actual_value = None
        else:
            if stock_actual_raw == "":
                flash("Debes indicar el stock actual.")
                return inventario_redirect_with_filters(producto_id=producto_id)
            try:
                stock_actual_value = float(stock_actual_raw.replace(",", "."))
            except ValueError:
                flash("Stock actual no válido. Debe ser un número.")
                return inventario_redirect_with_filters(producto_id=producto_id)

        update_product_fields(
            config.CSV_PRODUCTOS,
            backup_dir=config.BACKUP_DIR,
            producto_id=producto_id,
            fields={
                "nombre": nombre,
                "precio_unitario": precio_unitario,
                "unidad": unidad,
                "stock_minimo": stock_minimo,
                "grupo": grupo,
                "grupo_emoji": grupo_emoji,
                "stock_infinito": stock_infinito,
                "fraccionable": fraccionable,
                "fracciones_por_unidad": fracciones_por_unidad_clean,
                "unidad_venta": unidad_venta,
            },
        )
        if grupo_select == "__new__":
            create_group(
                config.CSV_GRUPOS,
                group_name=grupo,
                emoji=grupo_emoji_key,
                backup_dir=config.BACKUP_DIR,
            )
        if stock_actual_value is not None:
            set_stock_to_value(config.CSV_MOVS, producto_id, desired_stock=stock_actual_value)

        flash("Cambios guardados 💾")
        return inventario_redirect_with_filters(producto_id=producto_id)

    @app.post("/inventario/eliminar")
    def inventario_eliminar():
        r = require_admin()
        if r:
            return r

        producto_id = request.form.get("producto_id", "").strip()
        if not producto_id:
            flash("Producto no válido.")
            return inventario_redirect_with_filters()

        update_product_fields(
            config.CSV_PRODUCTOS,
            backup_dir=config.BACKUP_DIR,
            producto_id=producto_id,
            fields={"activo": "0"},
        )
        flash("Producto eliminado 🗑️")
        return inventario_redirect_with_filters()

    # -------- FACTURAS --------
    @app.get("/facturas/nueva")
    def nueva_factura():
        r = require_login()
        if r:
            return r

        current_filters = list_filters_from_request()
        prods = sorted(get_products(config.CSV_PRODUCTOS), key=lambda p: (p.get("nombre") or "").lower())
        stock_map = calc_stock_by_product(config.CSV_MOVS)

        product_options = []
        low_stock = 0
        infinite_stock = 0
        for p in prods:
            pid = p.get("producto_id", "")
            stock_unit = stock_unit_label(p)
            sale_unit = sale_unit_label(p)
            infinito = (p.get("stock_infinito", "0") == "1")
            current_stock = None if infinito else stock_map.get(pid, 0.0)
            group_name = stored_group_name(p.get("grupo"))
            group_emoji = display_group_emoji(p.get("grupo_emoji"))
            stock_display_main, stock_display_meta = stock_display_parts(p, current_stock)
            product_options.append({
                "producto_id": pid,
                "nombre": p.get("nombre", ""),
                "unidad_stock": stock_unit,
                "unidad_venta": sale_unit,
                "grupo": group_name,
                "grupo_emoji": group_emoji,
                "fraccionable": "1" if product_is_fractionable(p) else "0",
                "fracciones_por_unidad": format_compact_number(product_fraction_count(p)),
                "fraccion_info": fraction_info_label(p),
                "stock_infinito": "1" if infinito else "0",
                "stock_actual": current_stock,
                "stock_display_main": stock_display_main,
                "stock_display_meta": stock_display_meta,
                "precio_unitario": p.get("precio_unitario", ""),
                "stock_bajo": "1" if (current_stock is not None and current_stock < safe_float(p.get("stock_minimo"))) else "0",
                "stock_negative": "1" if (current_stock is not None and current_stock < 0) else "0",
                "stock_zero": "1" if (current_stock is not None and current_stock <= 0) else "0",
            })
            if infinito:
                infinite_stock += 1
            elif current_stock is not None and current_stock < safe_float(p.get("stock_minimo")):
                low_stock += 1
        group_options = group_options_for_products(prods)
        query = normalize_text_search(current_filters["q"])
        group_name = current_filters["g"]
        filtered_product_options = [
            row for row in product_options
            if (not query or query in normalize_text_search(row["nombre"]))
            and (not group_name or row["grupo"] == group_name)
        ]
        if config.LOW_RESOURCE_MODE:
            visible_product_options, product_browser = paginate_rows(
                filtered_product_options,
                per_page=24,
                endpoint="nueva_factura",
                current_filters=current_filters,
            )
        else:
            visible_product_options = filtered_product_options
            product_browser = simple_browser_state("nueva_factura", current_filters, len(filtered_product_options))

        default_invoice_product_row = find_default_invoice_product(prods)
        default_invoice_product = None
        if default_invoice_product_row:
            default_invoice_product = {
                "pid": default_invoice_product_row.get("producto_id", ""),
                "name": default_invoice_product_row.get("nombre", ""),
                "emoji": default_invoice_product_row.get("grupo_emoji", "") or "package",
                "stockUnit": stock_unit_label(default_invoice_product_row),
                "saleUnit": sale_unit_label(default_invoice_product_row),
                "fractional": product_is_fractionable(default_invoice_product_row),
                "fractions": format_compact_number(product_fraction_count(default_invoice_product_row)),
                "defaultPrice": default_invoice_product_row.get("precio_unitario", "") or "0",
            }

        if is_partial_json_request():
            return jsonify({
                "ok": True,
                "items": visible_product_options,
                "browser": product_browser,
                "summary": product_browser_summary(
                    product_browser,
                    "No hay productos que coincidan con el filtro.",
                ),
            })

        return render_template(
            "factura_nueva.html",
            title="Nueva factura",
            products=visible_product_options,
            group_options=group_options,
            today_iso=date.today().isoformat(),
            current_filters=current_filters,
            product_browser=product_browser,
            default_invoice_product=default_invoice_product,
            form_summary={
                "total_products": len(product_options),
                "groups": len(group_options),
                "low_stock": low_stock,
                "infinite_stock": infinite_stock,
            },
        )

    @app.post("/facturas/nueva")
    def nueva_factura_post():
        r = require_login()
        if r:
            return r

        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def fail(message: str, status: int = 400):
            if wants_json:
                return jsonify({"ok": False, "error": message}), status
            flash(message)
            return redirect_with_list_filters("nueva_factura")

        nota_global = (request.form.get("nota_global") or "").strip()
        invoice_date = (request.form.get("fecha_factura") or "").strip()

        if invoice_date:
            try:
                date.fromisoformat(invoice_date)
            except ValueError:
                return fail("Fecha de factura no válida.")
        else:
            invoice_date = date.today().isoformat()

        product_ids = request.form.getlist("producto_id[]")
        quantities = request.form.getlist("cantidad[]")
        prices = request.form.getlist("precio_unitario[]")

        max_len = max(len(product_ids), len(quantities), len(prices), 0)
        if max_len == 0:
            return fail("No se recibieron líneas de factura.")

        products = get_products(config.CSV_PRODUCTOS)
        product_map = {p.get("producto_id"): p for p in products}

        lines = []
        for i in range(max_len):
            pid = (product_ids[i] if i < len(product_ids) else "").strip()
            qty_raw = (quantities[i] if i < len(quantities) else "").strip()
            _price_raw = (prices[i] if i < len(prices) else "").strip()

            if not pid and not qty_raw and not _price_raw:
                continue

            if not pid:
                return fail(f"Línea {i+1}: debes seleccionar un producto.")
            product = product_map.get(pid)
            if not product:
                return fail(f"Línea {i+1}: producto no válido o inactivo.")

            if not qty_raw:
                return fail(f"Línea {i+1}: la cantidad es obligatoria.")
            try:
                qty = parse_decimal(qty_raw)
            except ValueError:
                return fail(f"Línea {i+1}: cantidad no válida.")
            if qty <= 0:
                return fail(f"Línea {i+1}: la cantidad debe ser mayor que 0.")

            try:
                price = round(parse_decimal(product.get("precio_unitario") or "0"), 2)
            except ValueError:
                return fail(f"Línea {i+1}: el producto {product.get('nombre', pid)} no tiene un precio válido en inventario.")
            if price < 0:
                return fail(f"Línea {i+1}: el producto {product.get('nombre', pid)} tiene un precio negativo en inventario.")

            lines.append({
                "producto_id": pid,
                "producto_nombre": product.get("nombre", ""),
                "unidad": sale_unit_label(product),
                "cantidad": str(qty),
                "stock_cantidad": str(sale_quantity_to_stock_quantity(product, qty)),
                "precio_unitario": f"{price:.2f}",
                "nota": "",
            })

        if not lines:
            return fail("Añade al menos una línea de factura válida.")

        factura_id, total = create_invoice(
            config.CSV_FACTURAS,
            config.CSV_FACTURA_LINEAS,
            config.CSV_MOVS,
            lines=lines,
            cliente="",
            nota_global=nota_global,
            invoice_date=invoice_date,
            user=session.get("user", ""),
        )
        if abs(total) > 1e-9:
            adjust_cash_balance(
                config.CSV_CAJA,
                delta=total,
                updated_by=session.get("user", ""),
                note=f"Factura {factura_id}",
            )
        print_warning = ""
        try:
            print_invoice_ticket(factura_id)
            if not wants_json:
                flash("Factura enviada a la impresora conectada.")
        except (ValueError, RuntimeError) as exc:
            print_warning = f"No se pudo imprimir la factura: {exc}"
            if not wants_json:
                flash(print_warning)

        success_message = f"Factura registrada ✅ ({len(lines)} líneas, total {total:.2f} €, ref: {factura_id})"
        if wants_json:
            return jsonify({
                "ok": True,
                "factura_id": factura_id,
                "total": f"{total:.2f}",
                "redirect_url": url_for("home"),
                "message": success_message,
                "print_warning": print_warning,
            })

        flash(success_message)
        return redirect(url_for("home"))

    @app.get("/facturas/historial")
    def historial_facturas():
        r = require_login()
        if r:
            return r

        invoices = list_invoices(config.CSV_FACTURAS, limit=0 if config.LOW_RESOURCE_MODE else 400)
        current_q = (request.args.get("q") or "").strip()
        current_from = (request.args.get("from") or "").strip()
        current_to = (request.args.get("to") or "").strip()

        query = current_q.lower()
        if query:
            invoices = [
                row for row in invoices
                if query in (row.get("factura_id", "").lower())
                or query in (row.get("cliente", "").lower())
                or query in (row.get("nota_global", "").lower())
                or query in (row.get("usuario", "").lower())
            ]
        if current_from or current_to:
            invoices = [
                row for row in invoices
                if matches_date_window(row.get("fecha", ""), current_from, current_to)
            ]

        if not user_is_admin():
            me = (session.get("user") or "").strip().lower()
            invoices = [
                row for row in invoices
                if (row.get("usuario") or "").strip().lower() == me
            ]

        if config.LOW_RESOURCE_MODE:
            visible_invoices, history_browser = paginate_rows(
                invoices,
                per_page=40,
                endpoint="historial_facturas",
                current_filters={
                    "q": current_q,
                    "from": current_from,
                    "to": current_to,
                },
            )
        else:
            visible_invoices = invoices
            history_browser = simple_browser_state(
                "historial_facturas",
                {
                    "q": current_q,
                    "from": current_from,
                    "to": current_to,
                },
                len(invoices),
            )

        invoice_ids = {
            (row.get("factura_id") or "").strip()
            for row in visible_invoices
            if (row.get("factura_id") or "").strip()
        }
        lines_map = list_invoice_lines_for_ids(config.CSV_FACTURA_LINEAS, invoice_ids)

        history_summary = {
            "count": len(invoices),
            "amount": sum(safe_float(row.get("total_importe")) for row in invoices),
            "lines": sum(int((row.get("lineas") or "0").strip() or "0") for row in invoices),
            "clients": len({(row.get("cliente") or "").strip() for row in invoices if (row.get("cliente") or "").strip()}),
        }

        return render_template(
            "facturas_historial.html",
            title="Historial facturas",
            invoices=visible_invoices,
            lines_map=lines_map,
            current_q=current_q,
            current_from=current_from,
            current_to=current_to,
            history_summary=history_summary,
            history_browser=history_browser,
        )

    @app.get("/facturas/historial/export")
    def export_historial_facturas():
        r = require_admin()
        if r:
            return r

        invoices = list_invoices(config.CSV_FACTURAS, limit=400)
        current_q = (request.args.get("q") or "").strip()
        current_from = (request.args.get("from") or "").strip()
        current_to = (request.args.get("to") or "").strip()

        query = current_q.lower()
        if query:
            invoices = [
                row for row in invoices
                if query in (row.get("factura_id", "").lower())
                or query in (row.get("cliente", "").lower())
                or query in (row.get("nota_global", "").lower())
                or query in (row.get("usuario", "").lower())
            ]
        if current_from or current_to:
            invoices = [
                row for row in invoices
                if matches_date_window(row.get("fecha", ""), current_from, current_to)
            ]
        rows = [
            {
                "fecha": row.get("fecha", ""),
                "factura_id": row.get("factura_id", ""),
                "cliente": row.get("cliente", ""),
                "usuario": row.get("usuario", ""),
                "lineas": row.get("lineas", ""),
                "total_importe": row.get("total_importe", ""),
                "nota_global": row.get("nota_global", ""),
            }
            for row in invoices
        ]
        return make_csv_response(
            "facturas_filtradas.csv",
            ["fecha", "factura_id", "cliente", "usuario", "lineas", "total_importe", "nota_global"],
            rows,
        )

    @app.post("/facturas/<factura_id>/imprimir")
    def imprimir_factura(factura_id: str):
        r = require_admin()
        if r:
            return r

        redirect_params = history_redirect_params()

        invoice = find_invoice(config.CSV_FACTURAS, factura_id)
        if not invoice:
            flash("Factura no encontrada.")
            return redirect(url_for("historial_facturas", **redirect_params))

        try:
            print_invoice_ticket(factura_id)
            flash(f"Factura {factura_id} enviada a la impresora.")
        except (ValueError, RuntimeError) as exc:
            flash(f"No se pudo imprimir la factura {factura_id}: {exc}")

        return redirect(url_for("historial_facturas", **redirect_params))

    @app.post("/facturas/<factura_id>/eliminar")
    def eliminar_factura(factura_id: str):
        r = require_admin()
        if r:
            return r

        redirect_params = history_redirect_params()
        deleted = delete_invoice(
            config.CSV_FACTURAS,
            config.CSV_FACTURA_LINEAS,
            config.CSV_MOVS,
            factura_id,
            backup_dir=config.BACKUP_DIR,
        )
        if not deleted:
            flash("Factura no encontrada.")
            return redirect(url_for("historial_facturas", **redirect_params))

        invoice = deleted["invoice"]
        amount = round(safe_float(invoice.get("total_importe")), 2)
        user = session.get("user", "")
        cash_updated = False
        if wants_cash_update() and abs(amount) > 1e-9:
            adjust_cash_balance(
                config.CSV_CAJA,
                delta=-amount,
                updated_by=user,
                note=f"Eliminación factura {factura_id}",
            )
            cash_updated = True

        refresh_cash_analysis_storage(force=True)
        log_action(
            config.CSV_LOGS,
            user,
            "ELIMINAR FACTURA",
            (
                f"ref={factura_id} | total={amount:.2f} | "
                f"lineas={deleted['removed_line_count']} | "
                f"movs={deleted['removed_movement_count']} | "
                f"actualiza_caja={'si' if cash_updated else 'no'}"
            ),
        )
        flash(
            "Factura eliminada ✅"
            + (f" Caja ajustada {-amount:.2f} €." if cash_updated else "")
        )
        return redirect(url_for("historial_facturas", **redirect_params))

    @app.get("/compras/historial")
    def historial_compras():
        r = require_admin()
        if r:
            return r

        prods = get_products(config.CSV_PRODUCTOS)
        product_names = {p.get("producto_id", ""): p.get("nombre", "") for p in prods}
        tickets, lines_map = list_purchase_history(
            config.CSV_MOVS,
            product_names=product_names,
            limit=0 if config.LOW_RESOURCE_MODE else 500,
        )
        current_q = (request.args.get("q") or "").strip()
        current_from = (request.args.get("from") or "").strip()
        current_to = (request.args.get("to") or "").strip()

        query = current_q.lower()
        if query:
            filtered = []
            for t in tickets:
                if (
                    query in (t.get("ref_id", "").lower())
                    or query in (t.get("proveedor", "").lower())
                    or query in (t.get("usuario", "").lower())
                ):
                    filtered.append(t)
            tickets = filtered
        if current_from or current_to:
            tickets = [
                t for t in tickets
                if matches_date_window(t.get("fecha", ""), current_from, current_to)
            ]
        if config.LOW_RESOURCE_MODE:
            visible_tickets, history_browser = paginate_rows(
                tickets,
                per_page=40,
                endpoint="historial_compras",
                current_filters={
                    "q": current_q,
                    "from": current_from,
                    "to": current_to,
                },
            )
        else:
            visible_tickets = tickets
            history_browser = simple_browser_state(
                "historial_compras",
                {
                    "q": current_q,
                    "from": current_from,
                    "to": current_to,
                },
                len(tickets),
            )
        visible_ticket_ids = {
            (ticket.get("ref_id") or "").strip()
            for ticket in visible_tickets
            if (ticket.get("ref_id") or "").strip()
        }
        lines_map = {
            ref_id: lines
            for ref_id, lines in lines_map.items()
            if ref_id in visible_ticket_ids
        }

        history_summary = {
            "count": len(tickets),
            "amount": sum(safe_float(t.get("total_importe")) for t in tickets),
            "units": sum(safe_float(t.get("unidades")) for t in tickets),
            "providers": len({(t.get("proveedor") or "").strip() for t in tickets if (t.get("proveedor") or "").strip()}),
        }

        return render_template(
            "compras_historial.html",
            title="Historial compras",
            tickets=visible_tickets,
            lines_map=lines_map,
            current_q=current_q,
            current_from=current_from,
            current_to=current_to,
            history_summary=history_summary,
            history_browser=history_browser,
        )

    @app.get("/compras/historial/export")
    def export_historial_compras():
        r = require_admin()
        if r:
            return r

        prods = get_products(config.CSV_PRODUCTOS)
        product_names = {p.get("producto_id", ""): p.get("nombre", "") for p in prods}
        tickets, _ = list_purchase_history(
            config.CSV_MOVS,
            product_names=product_names,
            limit=500,
        )
        current_q = (request.args.get("q") or "").strip()
        current_from = (request.args.get("from") or "").strip()
        current_to = (request.args.get("to") or "").strip()

        query = current_q.lower()
        if query:
            tickets = [
                t for t in tickets
                if query in (t.get("ref_id", "").lower())
                or query in (t.get("proveedor", "").lower())
                or query in (t.get("usuario", "").lower())
            ]
        if current_from or current_to:
            tickets = [
                t for t in tickets
                if matches_date_window(t.get("fecha", ""), current_from, current_to)
            ]

        rows = [
            {
                "fecha": row.get("fecha", ""),
                "ref_id": row.get("ref_id", ""),
                "proveedor": row.get("proveedor", ""),
                "usuario": row.get("usuario", ""),
                "lineas": row.get("lineas", ""),
                "unidades": row.get("unidades", ""),
                "total_importe": row.get("total_importe", ""),
            }
            for row in tickets
        ]
        return make_csv_response(
            "compras_filtradas.csv",
            ["fecha", "ref_id", "proveedor", "usuario", "lineas", "unidades", "total_importe"],
            rows,
        )

    @app.get("/supervision")
    def supervision():
        r = require_admin()
        if r:
            return r
        return redirect_to_config("logs-uso")

    @app.post("/compras/<ref_id>/eliminar")
    def eliminar_compra(ref_id: str):
        r = require_admin()
        if r:
            return r

        redirect_params = history_redirect_params()
        deleted = delete_purchase(
            config.CSV_MOVS,
            ref_id,
            backup_dir=config.BACKUP_DIR,
        )
        if not deleted:
            flash("Compra no encontrada.")
            return redirect(url_for("historial_compras", **redirect_params))

        amount = round(float(deleted["total_importe"]), 2)
        user = session.get("user", "")
        cash_updated = False
        if wants_cash_update() and abs(amount) > 1e-9:
            adjust_cash_balance(
                config.CSV_CAJA,
                delta=amount,
                updated_by=user,
                note=f"Eliminación compra {ref_id}",
            )
            cash_updated = True

        refresh_cash_analysis_storage(force=True)
        log_action(
            config.CSV_LOGS,
            user,
            "ELIMINAR COMPRA",
            (
                f"ref={ref_id} | total={amount:.2f} | "
                f"lineas={deleted['lineas']} | "
                f"actualiza_caja={'si' if cash_updated else 'no'}"
            ),
        )
        flash(
            "Compra eliminada ✅"
            + (f" Caja ajustada +{amount:.2f} €." if cash_updated else "")
        )
        return redirect(url_for("historial_compras", **redirect_params))

    @app.get("/supervision/export")
    def export_supervision():
        r = require_admin()
        if r:
            return r

        logs = admin_logs_context()["logs"]

        rows = [
            {
                "fecha": row.get("fecha", ""),
                "usuario": row.get("usuario", ""),
                "accion": row.get("accion", ""),
                "detalle": row.get("detalle", ""),
            }
            for row in logs
        ]
        return make_csv_response(
            "supervision_filtrada.csv",
            ["fecha", "usuario", "accion", "detalle"],
            rows,
        )

    @app.get("/compra/nueva")
    def nueva_compra():
        r = require_admin()
        if r:
            return r

        current_filters = list_filters_from_request()
        prods = sorted(get_products(config.CSV_PRODUCTOS), key=lambda p: (p.get("nombre") or "").lower())
        stock_map = calc_stock_by_product(config.CSV_MOVS)

        product_options = []
        low_stock = 0
        infinite_stock = 0
        for p in prods:
            pid = p.get("producto_id", "")
            stock_unit = stock_unit_label(p)
            sale_unit = sale_unit_label(p)
            infinito = (p.get("stock_infinito", "0") == "1")
            current_stock = None if infinito else stock_map.get(pid, 0.0)
            group_name = stored_group_name(p.get("grupo"))
            group_emoji = display_group_emoji(p.get("grupo_emoji"))
            stock_display_main, stock_display_meta = stock_display_parts(p, current_stock)
            product_options.append({
                "producto_id": pid,
                "nombre": p.get("nombre", ""),
                "unidad_stock": stock_unit,
                "unidad_venta": sale_unit,
                "grupo": group_name,
                "grupo_emoji": group_emoji,
                "precio_venta_actual": (p.get("precio_unitario") or "").strip(),
                "fraccionable": "1" if product_is_fractionable(p) else "0",
                "fracciones_por_unidad": format_compact_number(product_fraction_count(p)),
                "fraccion_info": fraction_info_label(p),
                "stock_infinito": "1" if infinito else "0",
                "stock_actual": current_stock,
                "stock_display_main": stock_display_main,
                "stock_display_meta": stock_display_meta,
                "stock_bajo": "1" if (current_stock is not None and current_stock < safe_float(p.get("stock_minimo"))) else "0",
                "stock_negative": "1" if (current_stock is not None and current_stock < 0) else "0",
                "stock_zero": "1" if (current_stock is not None and current_stock <= 0) else "0",
            })
            if infinito:
                infinite_stock += 1
            elif current_stock is not None and current_stock < safe_float(p.get("stock_minimo")):
                low_stock += 1
        group_options = group_options_for_products(prods)
        query = normalize_text_search(current_filters["q"])
        group_name = current_filters["g"]
        filtered_product_options = [
            row for row in product_options
            if (not query or query in normalize_text_search(row["nombre"]))
            and (not group_name or row["grupo"] == group_name)
        ]
        if config.LOW_RESOURCE_MODE:
            visible_product_options, product_browser = paginate_rows(
                filtered_product_options,
                per_page=24,
                endpoint="nueva_compra",
                current_filters=current_filters,
            )
        else:
            visible_product_options = filtered_product_options
            product_browser = simple_browser_state("nueva_compra", current_filters, len(filtered_product_options))

        if is_partial_json_request():
            return jsonify({
                "ok": True,
                "items": visible_product_options,
                "browser": product_browser,
                "summary": product_browser_summary(
                    product_browser,
                    "No hay productos que coincidan con el filtro.",
                ),
            })

        return render_template(
            "compra_nueva.html",
            title="Nueva compra",
            products=visible_product_options,
            group_options=group_options,
            today_iso=date.today().isoformat(),
            current_filters=current_filters,
            product_browser=product_browser,
            form_summary={
                "total_products": len(product_options),
                "groups": len(group_options),
                "low_stock": low_stock,
                "infinite_stock": infinite_stock,
            },
        )

    @app.post("/compra/nueva")
    def nueva_compra_post():
        r = require_admin()
        if r:
            return r
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def fail(message: str):
            if wants_json:
                return jsonify({"ok": False, "error": message}), 400
            flash(message)
            return redirect_with_list_filters("nueva_compra")

        global_note = (request.form.get("nota_global") or "").strip()
        purchase_date = (request.form.get("fecha_compra") or "").strip()

        if purchase_date:
            try:
                # formato esperado del input type="date"
                date.fromisoformat(purchase_date)
            except ValueError:
                return fail("Fecha de compra no válida.")
        else:
            purchase_date = date.today().isoformat()

        product_ids = request.form.getlist("producto_id[]")
        quantities = request.form.getlist("cantidad[]")
        prices = request.form.getlist("precio_compra[]")
        subtotals = request.form.getlist("subtotal_compra[]")
        sale_prices = request.form.getlist("precio_venta_actual[]")
        notes = request.form.getlist("nota_linea[]")

        max_len = max(len(product_ids), len(quantities), len(prices), len(subtotals), len(sale_prices), len(notes), 0)
        if max_len == 0:
            return fail("No se recibieron líneas de compra.")

        # Validación de productos activos existentes.
        product_map = {p.get("producto_id"): p for p in get_products(config.CSV_PRODUCTOS)}

        lines = []
        purchase_total = 0.0
        sale_price_updates: dict[str, str] = {}
        for i in range(max_len):
            pid = (product_ids[i] if i < len(product_ids) else "").strip()
            qty_raw = (quantities[i] if i < len(quantities) else "").strip()
            price_raw = (prices[i] if i < len(prices) else "").strip()
            subtotal_raw = (subtotals[i] if i < len(subtotals) else "").strip()
            sale_price_raw = (sale_prices[i] if i < len(sale_prices) else "").strip()
            note_raw = (notes[i] if i < len(notes) else "").strip()

            # línea completamente vacía -> se ignora
            if not pid and not qty_raw and not price_raw and not subtotal_raw and not sale_price_raw and not note_raw:
                continue

            if not pid:
                return fail(f"Línea {i+1}: debes seleccionar un producto.")
            if pid not in product_map:
                return fail(f"Línea {i+1}: producto no válido o inactivo.")
            if not qty_raw:
                return fail(f"Línea {i+1}: la cantidad es obligatoria.")

            try:
                qty = float(qty_raw.replace(",", "."))
            except ValueError:
                return fail(f"Línea {i+1}: cantidad no válida.")
            if qty <= 0:
                return fail(f"Línea {i+1}: la cantidad debe ser mayor que 0.")

            product = product_map[pid]
            price_clean = ""
            is_fractional = product_is_fractionable(product)
            price_source_raw = subtotal_raw if is_fractional else price_raw
            if price_source_raw:
                try:
                    entered_price = float(price_source_raw.replace(",", "."))
                except ValueError:
                    return fail(f"Línea {i+1}: {'subtotal' if is_fractional else 'precio de compra'} no válido.")
                if entered_price < 0:
                    return fail(f"Línea {i+1}: {'subtotal' if is_fractional else 'precio de compra'} no puede ser negativo.")
                pricing = purchase_price_breakdown(product, qty, entered_price)
                purchase_total += pricing["line_total"]
                price_clean = f"{pricing['unit_purchase_price']:.6f}".rstrip("0").rstrip(".")

            if not sale_price_raw:
                return fail(f"Línea {i+1}: el precio de venta actual es obligatorio.")

            try:
                sale_price = float(sale_price_raw.replace(",", "."))
            except ValueError:
                return fail(f"Línea {i+1}: precio de venta actual no válido.")
            if sale_price < 0:
                return fail(f"Línea {i+1}: precio de venta actual no puede ser negativo.")

            sale_price_clean = f"{sale_price:.2f}"

            lines.append({
                "producto_id": pid,
                "cantidad": str(qty),
                "precio_compra": price_clean,
                "nota": note_raw,
                "stock_infinito": product.get("stock_infinito", "0"),
            })
            sale_price_updates[pid] = sale_price_clean

        if not lines:
            return fail("Añade al menos una línea de compra válida.")

        ref_id = add_purchase_entries(
            config.CSV_MOVS,
            lines=lines,
            provider="",
            global_note=global_note,
            purchase_date=purchase_date,
            user=session.get("user", ""),
        )
        if abs(purchase_total) > 1e-9:
            adjust_cash_balance(
                config.CSV_CAJA,
                delta=-purchase_total,
                updated_by=session.get("user", ""),
                note=f"Compra {ref_id}",
            )
        update_many_product_fields(
            config.CSV_PRODUCTOS,
            backup_dir=config.BACKUP_DIR,
            updates={
                producto_id: {"precio_unitario": new_sale_price}
                for producto_id, new_sale_price in sale_price_updates.items()
            },
        )
        success_message = f"Compra registrada ✅ ({len(lines)} líneas, ref: {ref_id})"
        if wants_json:
            return jsonify({
                "ok": True,
                "ref_id": ref_id,
                "total": f"{purchase_total:.2f}",
                "redirect_url": url_for("home"),
                "message": success_message,
            })
        flash(success_message)
        return redirect(url_for("home"))

    @app.get("/gastos/nuevo")
    def nuevo_gasto_libre():
        r = require_admin()
        if r:
            return r

        return render_template(
            "gasto_nuevo.html",
            title="Nuevo gasto libre",
            today_iso=date.today().isoformat(),
            categories=[
                {"value": category, "label": free_expense_category_label(category)}
                for category in FREE_EXPENSE_CATEGORIES
            ],
        )

    @app.post("/gastos/nuevo")
    def nuevo_gasto_libre_post():
        r = require_admin()
        if r:
            return r
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def fail(message: str):
            if wants_json:
                return jsonify({"ok": False, "error": message}), 400
            flash(message)
            return redirect(url_for("nuevo_gasto_libre"))

        expense_date = (request.form.get("fecha_gasto") or "").strip()
        category = (request.form.get("categoria") or "").strip()
        description = (request.form.get("descripcion") or "").strip()
        amount_raw = (request.form.get("importe") or "").strip()

        if expense_date:
            try:
                date.fromisoformat(expense_date)
            except ValueError:
                return fail("Fecha de gasto no válida.")
        else:
            expense_date = date.today().isoformat()

        if category not in FREE_EXPENSE_CATEGORIES:
            return fail("Categoría de gasto no válida.")

        if not description:
            return fail("La descripción es obligatoria.")

        try:
            amount = round(parse_decimal(amount_raw), 2)
        except ValueError:
            return fail("Importe no válido.")
        if amount <= 0:
            return fail("El importe debe ser mayor que 0.")

        expense_id, total = create_free_expense(
            config.CSV_GASTOS_LIBRES,
            amount=amount,
            description=description,
            category=category,
            expense_date=expense_date,
            user=session.get("user", ""),
        )
        adjust_cash_balance(
            config.CSV_CAJA,
            delta=-total,
            updated_by=session.get("user", ""),
            note=f"Gasto libre {expense_id}",
        )
        success_message = f"Gasto libre registrado ✅ ({total:.2f} €, ref: {expense_id})"
        if wants_json:
            return jsonify({
                "ok": True,
                "expense_id": expense_id,
                "total": f"{total:.2f}",
                "redirect_url": url_for("home"),
                "message": success_message,
            })
        flash(success_message)
        return redirect(url_for("nuevo_gasto_libre"))

    @app.get("/gastos/historial")
    def historial_gastos_libres():
        r = require_admin()
        if r:
            return r

        expenses = list_free_expenses(config.CSV_GASTOS_LIBRES, limit=page_limit(500, 150))
        current_q = (request.args.get("q") or "").strip()
        current_from = (request.args.get("from") or "").strip()
        current_to = (request.args.get("to") or "").strip()

        query = current_q.lower()
        if query:
            expenses = [
                row for row in expenses
                if query in (row.get("gasto_id", "").lower())
                or query in (row.get("descripcion", "").lower())
                or query in (row.get("categoria", "").lower())
                or query in (row.get("usuario", "").lower())
            ]
        if current_from or current_to:
            expenses = [
                row for row in expenses
                if matches_date_window(row.get("fecha", ""), current_from, current_to)
            ]

        expenses = [
            {
                **row,
                "categoria_label": free_expense_category_label(row.get("categoria", "")),
            }
            for row in expenses
        ]

        history_summary = {
            "shown": len(expenses),
            "amount": sum(safe_float(row.get("importe")) for row in expenses),
            "users": len({(row.get("usuario") or "").strip() for row in expenses if (row.get("usuario") or "").strip()}),
        }

        return render_template(
            "gastos_historial.html",
            title="Historial de gastos libres",
            expenses=expenses,
            current_q=current_q,
            current_from=current_from,
            current_to=current_to,
            history_summary=history_summary,
        )

    @app.get("/gastos/historial/export")
    def export_historial_gastos_libres():
        r = require_admin()
        if r:
            return r

        expenses = list_free_expenses(config.CSV_GASTOS_LIBRES, limit=500)
        current_q = (request.args.get("q") or "").strip()
        current_from = (request.args.get("from") or "").strip()
        current_to = (request.args.get("to") or "").strip()

        query = current_q.lower()
        if query:
            expenses = [
                row for row in expenses
                if query in (row.get("gasto_id", "").lower())
                or query in (row.get("descripcion", "").lower())
                or query in (row.get("categoria", "").lower())
                or query in (row.get("usuario", "").lower())
            ]
        if current_from or current_to:
            expenses = [
                row for row in expenses
                if matches_date_window(row.get("fecha", ""), current_from, current_to)
            ]

        rows = [
            {
                "fecha": row.get("fecha", ""),
                "gasto_id": row.get("gasto_id", ""),
                "categoria": row.get("categoria", ""),
                "descripcion": row.get("descripcion", ""),
                "importe": row.get("importe", ""),
                "usuario": row.get("usuario", ""),
            }
            for row in expenses
        ]
        return make_csv_response(
            "gastos_libres_filtrados.csv",
            ["fecha", "gasto_id", "categoria", "descripcion", "importe", "usuario"],
            rows,
        )

    @app.post("/gastos/<expense_id>/eliminar")
    def eliminar_gasto_libre(expense_id: str):
        r = require_admin()
        if r:
            return r

        redirect_params = history_redirect_params()
        deleted = delete_free_expense(
            config.CSV_GASTOS_LIBRES,
            expense_id,
            backup_dir=config.BACKUP_DIR,
        )
        if not deleted:
            flash("Gasto no encontrado.")
            return redirect(url_for("historial_gastos_libres", **redirect_params))

        amount = round(safe_float(deleted.get("importe")), 2)
        user = session.get("user", "")
        cash_updated = False
        if wants_cash_update() and abs(amount) > 1e-9:
            adjust_cash_balance(
                config.CSV_CAJA,
                delta=amount,
                updated_by=user,
                note=f"Eliminación gasto libre {expense_id}",
            )
            cash_updated = True

        refresh_cash_analysis_storage(force=True)
        log_action(
            config.CSV_LOGS,
            user,
            "ELIMINAR GASTO",
            (
                f"ref={expense_id} | total={amount:.2f} | "
                f"categoria={(deleted.get('categoria') or '').strip()} | "
                f"actualiza_caja={'si' if cash_updated else 'no'}"
            ),
        )
        flash(
            "Gasto eliminado ✅"
            + (f" Caja ajustada +{amount:.2f} €." if cash_updated else "")
        )
        return redirect(url_for("historial_gastos_libres", **redirect_params))

    return app

if __name__ == "__main__":
    app = create_app()
    debug_enabled = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_enabled)
