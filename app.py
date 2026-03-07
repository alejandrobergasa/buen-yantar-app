from __future__ import annotations

from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from datetime import date
import os
import config

from services.csv_store import ensure_csv
from services.audit import AUDIT_HEADERS, log_action, list_logs
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
from services.purchases import list_purchase_history
from services.inventory import (
    PRODUCT_HEADERS, MOV_HEADERS,
    ensure_product_schema,
    ensure_demo_products,
    get_products, find_product,
    calc_stock_by_product, last_purchases_for_product,
    update_product_fields, set_stock_to_value,
    create_product, add_purchase_entries
)
from services.invoices import (
    INVOICE_HEADERS, INVOICE_LINE_HEADERS,
    create_invoice, list_invoices, list_invoice_lines
)

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
    ensure_csv(config.CSV_MOVS, MOV_HEADERS)
    ensure_csv(config.CSV_FACTURAS, INVOICE_HEADERS)
    ensure_csv(config.CSV_FACTURA_LINEAS, INVOICE_LINE_HEADERS)
    ensure_csv(config.CSV_LOGS, AUDIT_HEADERS)
    ensure_user_schema(config.CSV_USUARIOS, backup_dir=config.BACKUP_DIR)
    ensure_product_schema(config.CSV_PRODUCTOS, backup_dir=config.BACKUP_DIR)

    ensure_default_admin(config.CSV_USUARIOS)
    if os.getenv("LOAD_DEMO_DATA", "0") == "1":
        ensure_demo_products(config.CSV_PRODUCTOS)

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

    @app.before_request
    def inject_user_context():
        u = current_user_row()
        g.current_user = u
        g.is_admin = is_admin(u)

    def inventario_redirect_with_filters(producto_id: str | None = None):
        q = (request.form.get("f_q") or request.args.get("q") or "").strip()
        g = (request.form.get("f_g") or request.args.get("g") or "").strip()
        low = "1" if (request.form.get("f_low") or request.args.get("low")) == "1" else "0"

        params = {}
        if producto_id:
            params["producto_id"] = producto_id
        if q:
            params["q"] = q
        if g:
            params["g"] = g
        if low == "1":
            params["low"] = "1"

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

    @app.after_request
    def audit_every_request(response):
        endpoint = (request.endpoint or "").strip()
        if endpoint == "static" or request.path.startswith("/static/"):
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
        session.clear()
        return redirect(url_for("login"))

    @app.get("/usuarios/gestion")
    def gestion_usuarios():
        r = require_login()
        if r:
            return r
        users = list_users(config.CSV_USUARIOS) if user_is_admin() else []
        return render_template(
            "usuarios_gestion.html",
            title="Gestion de usuario",
            is_admin=user_is_admin(),
            users=users,
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
            return redirect(url_for("gestion_usuarios"))
        if len(new_password) < 4:
            flash("La nueva contraseña debe tener al menos 4 caracteres.")
            return redirect(url_for("gestion_usuarios"))
        if new_password != repeat_password:
            flash("La nueva contraseña y la repetición no coinciden.")
            return redirect(url_for("gestion_usuarios"))

        update_password(
            config.CSV_USUARIOS,
            backup_dir=config.BACKUP_DIR,
            username=username,
            new_password=new_password,
        )
        flash("Contraseña actualizada ✅")
        return redirect(url_for("gestion_usuarios"))

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
            return redirect(url_for("gestion_usuarios"))
        if len(password) < 4:
            flash("La contraseña debe tener al menos 4 caracteres.")
            return redirect(url_for("gestion_usuarios"))

        try:
            create_user(config.CSV_USUARIOS, username=username, password=password, rol=role)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("gestion_usuarios"))

        flash("Usuario creado ✅")
        return redirect(url_for("gestion_usuarios"))

    @app.post("/usuarios/eliminar")
    def eliminar_usuario():
        r = require_admin()
        if r:
            return r

        username = (request.form.get("username") or "").strip()
        if not username:
            flash("Usuario no válido.")
            return redirect(url_for("gestion_usuarios"))

        me = (session.get("user") or "").strip().lower()
        if username.lower() == me:
            flash("No puedes desactivar tu propio usuario.")
            return redirect(url_for("gestion_usuarios"))

        users = list_users(config.CSV_USUARIOS)
        target = next((u for u in users if (u.get("username") or "").strip().lower() == username.lower()), None)
        if not target:
            flash("Usuario no encontrado.")
            return redirect(url_for("gestion_usuarios"))

        try:
            delete_user(config.CSV_USUARIOS, backup_dir=config.BACKUP_DIR, username=username)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("gestion_usuarios"))

        flash("Usuario eliminado 🗑️")
        return redirect(url_for("gestion_usuarios"))

    @app.get("/")
    def root():
        return redirect(url_for("home"))

    @app.get("/home")
    def home():
        r = require_login()
        if r:
            return r
        return render_template("home.html", title="Pantalla principal")

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

        prods = get_products(config.CSV_PRODUCTOS)
        stock_map = calc_stock_by_product(config.CSV_MOVS)

        # grupos únicos para el desplegable
        groups_map = {}
        for p in prods:
            g = (p.get("grupo") or "Otros").strip()
            e = (p.get("grupo_emoji") or "📦").strip()
            groups_map[g] = e
        group_options = sorted([(g, groups_map[g]) for g in groups_map], key=lambda x: x[0].lower())

        # tabla/listado (incluye bandera bajo mínimo y stock infinito)
        rows = []
        for p in prods:
            pid = p["producto_id"]
            infinito = (p.get("stock_infinito", "0") == "1")

            stock = stock_map.get(pid, 0.0)
            stock_min = float(p.get("stock_minimo") or 0.0)
            bajo_min = (False if infinito else (stock < stock_min))

            rows.append({
                "producto_id": pid,
                "nombre": p.get("nombre", ""),
                "unidad": p.get("unidad", "ud"),
                "grupo": p.get("grupo", "Otros"),
                "grupo_emoji": p.get("grupo_emoji", "📦"),
                "stock_infinito": "1" if infinito else "0",
                "stock_minimo": str(p.get("stock_minimo", "0")),
                "total": None if infinito else stock,
                "bajo_minimo": "1" if bajo_min else "0",
            })
        rows.sort(key=lambda x: x["nombre"].lower())

        selected = find_product(config.CSV_PRODUCTOS, producto_id) if producto_id else None

        selected_stock = None
        selected_purchases = []
        if selected:
            sel_inf = (selected.get("stock_infinito", "0") == "1")
            selected_stock = None if sel_inf else stock_map.get(producto_id, 0.0)
            selected_purchases = last_purchases_for_product(config.CSV_MOVS, producto_id, limit=300)

        return render_template(
            "inventario.html",
            title="Inventario",
            products_table=rows,
            selected=selected,
            selected_stock=selected_stock,
            selected_purchases=selected_purchases,
            group_options=group_options,
            current_filters=current_filters,
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

        grupo_select = (request.form.get("grupo_select") or "").strip()

        # Construimos un mapa de grupos existentes -> emoji a partir del inventario actual
        prods = get_products(config.CSV_PRODUCTOS)
        groups_map = {}
        for p in prods:
            g = (p.get("grupo") or "Otros").strip()
            e = (p.get("grupo_emoji") or "📦").strip()
            groups_map[g] = e

        if grupo_select == "__new__":
            grupo = (request.form.get("grupo_custom") or "").strip()
            grupo_emoji = (request.form.get("grupo_emoji_custom") or "").strip()
            if not grupo:
                flash("Si eliges 'Nuevo grupo', debes indicar el nombre del grupo.")
                return inventario_redirect_with_filters()
            if not grupo_emoji:
                flash("Si eliges 'Nuevo grupo', debes indicar el emoji del grupo.")
                return inventario_redirect_with_filters()
        else:
            grupo = grupo_select or "Otros"
            grupo_emoji = groups_map.get(grupo, "📦")

        if not nombre:
            flash("El nombre del producto es obligatorio.")
            return inventario_redirect_with_filters()

        stock_actual_value = None
        if stock_infinito == "0":
            if stock_actual == "":
                flash("Si el producto no es infinito, debes indicar el stock actual.")
                return inventario_redirect_with_filters()
            try:
                stock_actual_value = float(stock_actual.replace(",", "."))
                if stock_actual_value < 0:
                    raise ValueError
            except ValueError:
                flash("Stock actual no válido. Debe ser un número mayor o igual que 0.")
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
        )
        if stock_actual_value is not None:
            set_stock_to_value(config.CSV_MOVS, pid, desired_stock=stock_actual_value)

        flash("Producto añadido ✅")
        return inventario_redirect_with_filters(producto_id=pid)

    @app.post("/inventario/editar")
    def inventario_editar():
        r = require_login()
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

        grupo = request.form.get("grupo", "Otros").strip() or "Otros"
        grupo_emoji = request.form.get("grupo_emoji", "📦").strip() or "📦"

        stock_infinito = "1" if request.form.get("stock_infinito") == "on" else "0"
        if stock_infinito == "1":
            stock_minimo = "0"
            stock_actual_value = None
        else:
            if stock_actual_raw == "":
                flash("Debes indicar el stock actual.")
                return inventario_redirect_with_filters(producto_id=producto_id)
            try:
                stock_actual_value = float(stock_actual_raw.replace(",", "."))
                if stock_actual_value < 0:
                    raise ValueError
            except ValueError:
                flash("Stock actual no válido. Debe ser un número mayor o igual que 0.")
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
            },
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
        prods = get_products(config.CSV_PRODUCTOS)
        prods.sort(key=lambda p: (p.get("nombre") or "").lower())
        stock_map = calc_stock_by_product(config.CSV_MOVS)

        product_options = []
        groups_map = {}
        for p in prods:
            pid = p.get("producto_id", "")
            unit = p.get("unidad", "ud")
            infinito = (p.get("stock_infinito", "0") == "1")
            current_stock = None if infinito else stock_map.get(pid, 0.0)
            group_name = p.get("grupo", "Otros")
            group_emoji = p.get("grupo_emoji", "📦")
            groups_map[group_name] = group_emoji
            product_options.append({
                "producto_id": pid,
                "nombre": p.get("nombre", ""),
                "unidad": unit,
                "grupo": group_name,
                "grupo_emoji": group_emoji,
                "stock_infinito": "1" if infinito else "0",
                "stock_actual": current_stock,
                "precio_unitario": p.get("precio_unitario", ""),
            })
        group_options = sorted([(g, groups_map[g]) for g in groups_map], key=lambda x: x[0].lower())

        return render_template(
            "factura_nueva.html",
            title="Nueva factura",
            products=product_options,
            group_options=group_options,
            today_iso=date.today().isoformat(),
            current_filters=current_filters,
        )

    @app.post("/facturas/nueva")
    def nueva_factura_post():
        r = require_login()
        if r:
            return r

        cliente = (request.form.get("cliente") or "").strip()
        nota_global = (request.form.get("nota_global") or "").strip()
        invoice_date = (request.form.get("fecha_factura") or "").strip()

        if invoice_date:
            try:
                date.fromisoformat(invoice_date)
            except ValueError:
                flash("Fecha de factura no válida.")
                return redirect_with_list_filters("nueva_factura")
        else:
            invoice_date = date.today().isoformat()

        product_ids = request.form.getlist("producto_id[]")
        quantities = request.form.getlist("cantidad[]")
        prices = request.form.getlist("precio_unitario[]")
        notes = request.form.getlist("nota_linea[]")

        max_len = max(len(product_ids), len(quantities), len(prices), len(notes), 0)
        if max_len == 0:
            flash("No se recibieron líneas de factura.")
            return redirect_with_list_filters("nueva_factura")

        products = get_products(config.CSV_PRODUCTOS)
        product_map = {p.get("producto_id"): p for p in products}
        stock_map = calc_stock_by_product(config.CSV_MOVS)
        consumed_by_pid: dict[str, float] = {}

        lines = []
        for i in range(max_len):
            pid = (product_ids[i] if i < len(product_ids) else "").strip()
            qty_raw = (quantities[i] if i < len(quantities) else "").strip()
            price_raw = (prices[i] if i < len(prices) else "").strip()
            note_raw = (notes[i] if i < len(notes) else "").strip()

            if not pid and not qty_raw and not price_raw and not note_raw:
                continue

            if not pid:
                flash(f"Línea {i+1}: debes seleccionar un producto.")
                return redirect_with_list_filters("nueva_factura")
            product = product_map.get(pid)
            if not product:
                flash(f"Línea {i+1}: producto no válido o inactivo.")
                return redirect_with_list_filters("nueva_factura")

            if not qty_raw:
                flash(f"Línea {i+1}: la cantidad es obligatoria.")
                return redirect_with_list_filters("nueva_factura")
            try:
                qty = parse_decimal(qty_raw)
            except ValueError:
                flash(f"Línea {i+1}: cantidad no válida.")
                return redirect_with_list_filters("nueva_factura")
            if qty <= 0:
                flash(f"Línea {i+1}: la cantidad debe ser mayor que 0.")
                return redirect_with_list_filters("nueva_factura")

            if not price_raw:
                flash(f"Línea {i+1}: el precio unitario es obligatorio.")
                return redirect_with_list_filters("nueva_factura")
            try:
                price = parse_decimal(price_raw)
            except ValueError:
                flash(f"Línea {i+1}: precio unitario no válido.")
                return redirect_with_list_filters("nueva_factura")
            if price < 0:
                flash(f"Línea {i+1}: precio unitario no puede ser negativo.")
                return redirect_with_list_filters("nueva_factura")

            is_inf = (product.get("stock_infinito", "0") == "1")
            if not is_inf:
                available = stock_map.get(pid, 0.0) - consumed_by_pid.get(pid, 0.0)
                if qty > available + 1e-9:
                    pname = product.get("nombre", pid)
                    avail_text = f"{available:.2f}".rstrip("0").rstrip(".")
                    flash(f"Línea {i+1}: stock insuficiente para {pname}. Disponible: {avail_text}.")
                    return redirect_with_list_filters("nueva_factura")
                consumed_by_pid[pid] = consumed_by_pid.get(pid, 0.0) + qty

            lines.append({
                "producto_id": pid,
                "producto_nombre": product.get("nombre", ""),
                "unidad": product.get("unidad", "ud"),
                "cantidad": str(qty),
                "precio_unitario": f"{price:.2f}",
                "nota": note_raw,
            })

        if not lines:
            flash("Añade al menos una línea de factura válida.")
            return redirect_with_list_filters("nueva_factura")

        factura_id, total = create_invoice(
            config.CSV_FACTURAS,
            config.CSV_FACTURA_LINEAS,
            config.CSV_MOVS,
            lines=lines,
            cliente=cliente,
            nota_global=nota_global,
            invoice_date=invoice_date,
            user=session.get("user", ""),
        )
        flash(f"Factura registrada ✅ ({len(lines)} líneas, total {total:.2f} €, ref: {factura_id})")
        return redirect_with_list_filters("nueva_factura")

    @app.get("/facturas/historial")
    def historial_facturas():
        r = require_login()
        if r:
            return r

        invoices = list_invoices(config.CSV_FACTURAS, limit=400)
        raw_lines = list_invoice_lines(config.CSV_FACTURA_LINEAS)

        lines_map: dict[str, list[dict[str, str]]] = {}
        for line in raw_lines:
            fid = (line.get("factura_id") or "").strip()
            if not fid:
                continue
            lines_map.setdefault(fid, []).append(line)

        query = (request.args.get("q") or "").strip().lower()
        if query:
            invoices = [
                row for row in invoices
                if query in (row.get("factura_id", "").lower())
                or query in (row.get("cliente", "").lower())
                or query in (row.get("nota_global", "").lower())
                or query in (row.get("usuario", "").lower())
            ]

        if not user_is_admin():
            me = (session.get("user") or "").strip().lower()
            invoices = [
                row for row in invoices
                if (row.get("usuario") or "").strip().lower() == me
            ]

        return render_template(
            "facturas_historial.html",
            title="Historial facturas",
            invoices=invoices,
            lines_map=lines_map,
            current_q=(request.args.get("q") or "").strip(),
        )

    @app.get("/compras/historial")
    def historial_compras():
        r = require_login()
        if r:
            return r

        prods = get_products(config.CSV_PRODUCTOS)
        product_names = {p.get("producto_id", ""): p.get("nombre", "") for p in prods}
        tickets, lines_map = list_purchase_history(
            config.CSV_MOVS,
            product_names=product_names,
            limit=500,
        )

        query = (request.args.get("q") or "").strip().lower()
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

        return render_template(
            "compras_historial.html",
            title="Historial compras",
            tickets=tickets,
            lines_map=lines_map,
            current_q=(request.args.get("q") or "").strip(),
        )

    @app.get("/supervision")
    def supervision():
        r = require_admin()
        if r:
            return r

        all_logs = list_logs(config.CSV_LOGS, limit=2000)
        logs = list(all_logs)
        q = (request.args.get("q") or "").strip().lower()
        user = (request.args.get("user") or "").strip().lower()

        if q:
            logs = [
                row for row in logs
                if q in (row.get("accion", "").lower()) or q in (row.get("detalle", "").lower())
            ]
        if user:
            logs = [row for row in logs if user in (row.get("usuario", "").lower())]

        users = sorted({(row.get("usuario") or "").strip() for row in all_logs if (row.get("usuario") or "").strip()})
        return render_template(
            "supervision.html",
            title="Supervision",
            logs=logs,
            current_q=(request.args.get("q") or "").strip(),
            current_user=(request.args.get("user") or "").strip(),
            user_options=users,
        )

    @app.get("/compra/nueva")
    def nueva_compra():
        r = require_login()
        if r:
            return r

        current_filters = list_filters_from_request()
        prods = get_products(config.CSV_PRODUCTOS)
        prods.sort(key=lambda p: (p.get("nombre") or "").lower())
        stock_map = calc_stock_by_product(config.CSV_MOVS)

        product_options = []
        groups_map = {}
        for p in prods:
            pid = p.get("producto_id", "")
            unit = p.get("unidad", "ud")
            infinito = (p.get("stock_infinito", "0") == "1")
            current_stock = None if infinito else stock_map.get(pid, 0.0)
            group_name = p.get("grupo", "Otros")
            group_emoji = p.get("grupo_emoji", "📦")
            groups_map[group_name] = group_emoji
            product_options.append({
                "producto_id": pid,
                "nombre": p.get("nombre", ""),
                "unidad": unit,
                "grupo": group_name,
                "grupo_emoji": group_emoji,
                "stock_infinito": "1" if infinito else "0",
                "stock_actual": current_stock,
            })
        group_options = sorted([(g, groups_map[g]) for g in groups_map], key=lambda x: x[0].lower())

        return render_template(
            "compra_nueva.html",
            title="Nueva compra",
            products=product_options,
            group_options=group_options,
            today_iso=date.today().isoformat(),
            current_filters=current_filters,
        )

    @app.post("/compra/nueva")
    def nueva_compra_post():
        r = require_login()
        if r:
            return r

        provider = (request.form.get("proveedor") or "").strip()
        global_note = (request.form.get("nota_global") or "").strip()
        purchase_date = (request.form.get("fecha_compra") or "").strip()

        if purchase_date:
            try:
                # formato esperado del input type="date"
                date.fromisoformat(purchase_date)
            except ValueError:
                flash("Fecha de compra no válida.")
                return redirect_with_list_filters("nueva_compra")
        else:
            purchase_date = date.today().isoformat()

        product_ids = request.form.getlist("producto_id[]")
        quantities = request.form.getlist("cantidad[]")
        prices = request.form.getlist("precio_compra[]")
        notes = request.form.getlist("nota_linea[]")

        max_len = max(len(product_ids), len(quantities), len(prices), len(notes), 0)
        if max_len == 0:
            flash("No se recibieron líneas de compra.")
            return redirect_with_list_filters("nueva_compra")

        # Validación de productos activos existentes.
        product_map = {p.get("producto_id"): p for p in get_products(config.CSV_PRODUCTOS)}

        lines = []
        for i in range(max_len):
            pid = (product_ids[i] if i < len(product_ids) else "").strip()
            qty_raw = (quantities[i] if i < len(quantities) else "").strip()
            price_raw = (prices[i] if i < len(prices) else "").strip()
            note_raw = (notes[i] if i < len(notes) else "").strip()

            # línea completamente vacía -> se ignora
            if not pid and not qty_raw and not price_raw and not note_raw:
                continue

            if not pid:
                flash(f"Línea {i+1}: debes seleccionar un producto.")
                return redirect_with_list_filters("nueva_compra")
            if pid not in product_map:
                flash(f"Línea {i+1}: producto no válido o inactivo.")
                return redirect_with_list_filters("nueva_compra")
            if (product_map[pid].get("stock_infinito", "0") == "1"):
                flash(f"Línea {i+1}: no puedes registrar compras de productos con stock infinito.")
                return redirect_with_list_filters("nueva_compra")

            if not qty_raw:
                flash(f"Línea {i+1}: la cantidad es obligatoria.")
                return redirect_with_list_filters("nueva_compra")

            try:
                qty = float(qty_raw.replace(",", "."))
            except ValueError:
                flash(f"Línea {i+1}: cantidad no válida.")
                return redirect_with_list_filters("nueva_compra")
            if qty <= 0:
                flash(f"Línea {i+1}: la cantidad debe ser mayor que 0.")
                return redirect_with_list_filters("nueva_compra")

            price_clean = ""
            if price_raw:
                try:
                    price = float(price_raw.replace(",", "."))
                except ValueError:
                    flash(f"Línea {i+1}: precio de compra no válido.")
                    return redirect_with_list_filters("nueva_compra")
                if price < 0:
                    flash(f"Línea {i+1}: precio de compra no puede ser negativo.")
                    return redirect_with_list_filters("nueva_compra")
                price_clean = f"{price:.2f}"

            lines.append({
                "producto_id": pid,
                "cantidad": str(qty),
                "precio_compra": price_clean,
                "nota": note_raw,
            })

        if not lines:
            flash("Añade al menos una línea de compra válida.")
            return redirect_with_list_filters("nueva_compra")

        ref_id = add_purchase_entries(
            config.CSV_MOVS,
            lines=lines,
            provider=provider,
            global_note=global_note,
            purchase_date=purchase_date,
            user=session.get("user", ""),
        )
        flash(f"Compra registrada ✅ ({len(lines)} líneas, ref: {ref_id})")
        return redirect_with_list_filters("nueva_compra")

    return app

if __name__ == "__main__":
    app = create_app()
    debug_enabled = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_enabled)
