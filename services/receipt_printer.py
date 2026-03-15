from __future__ import annotations

from datetime import datetime
from pathlib import Path
import ctypes
import os
from textwrap import wrap
from uuid import uuid4


TICKET_WIDTH = 32
RAW_NEWLINE = "\r\n"


def _center(text: str, width: int = TICKET_WIDTH) -> str:
    return (text or "").strip()[:width].center(width)


def _rule(char: str = "-", width: int = TICKET_WIDTH) -> str:
    return char * width


def _money(raw: str) -> str:
    try:
        return f"{float(raw or 0):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return "0,00"


def _wrap_line(text: str, width: int = TICKET_WIDTH) -> list[str]:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return [""]
    return wrap(cleaned, width=width, break_long_words=True, break_on_hyphens=False) or [cleaned[:width]]


def _fit(text: str, width: int = TICKET_WIDTH) -> list[str]:
    return _wrap_line(text, width)


def _clean_inline(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _truncate_with_ellipsis(text: str, max_len: int) -> str:
    clean = _clean_inline(text)
    if max_len <= 0:
        return ""
    if len(clean) <= max_len:
        return clean
    if max_len <= 3:
        return "." * max_len
    return f"{clean[:max_len - 3]}..."


def _qty_display(raw: str, width: int = 3) -> str:
    text = _clean_inline(raw)
    candidates: list[str] = []
    try:
        value = float(text.replace(",", "."))
        if abs(value - int(value)) < 1e-9:
            candidates.append(str(int(value)))
        candidates.extend([
            f"{value:.1f}".rstrip("0").rstrip("."),
            f"{value:.0f}",
        ])
    except (TypeError, ValueError):
        pass

    if text:
        candidates.append(text)

    seen: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)

    for candidate in seen:
        if len(candidate) <= width:
            return candidate.rjust(width)

    return _truncate_with_ellipsis(seen[0] if seen else "", width).rjust(width)


def _format_label_value(label: str, value: str, width: int = TICKET_WIDTH, min_dots: int = 2) -> list[str]:
    clean_label = _clean_inline(label)
    clean_value = _clean_inline(value)
    prefix = f"{clean_label} "

    if not clean_value:
        return [_fit(prefix.strip(), width)[0]]

    min_space = len(prefix) + min_dots + 1 + len(clean_value)
    if min_space <= width:
        dots = "." * max(min_dots, width - len(prefix) - len(clean_value) - 1)
        return [f"{prefix}{dots} {clean_value}"]

    first_width = max(1, width - len(prefix))
    wrapped_value = _wrap_line(clean_value, first_width)
    lines = [f"{prefix}{wrapped_value[0]}"]
    indent = " " * len(prefix)
    for part in wrapped_value[1:]:
        lines.append(f"{indent}{part}")
    return lines


def _format_note_block(note: str, width: int = TICKET_WIDTH) -> list[str]:
    clean_note = _clean_inline(note)
    if not clean_note:
        return []

    prefix = "Nota: "
    available = max(1, width - len(prefix))
    wrapped = wrap(
        clean_note,
        width=available,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [clean_note[:available]]
    lines = [f"{prefix}{wrapped[0]}"]
    indent = " " * len(prefix)
    for part in wrapped[1:]:
        lines.append(f"{indent}{part}")
    return lines


def _format_product_ticket_block(line: dict[str, str], width: int = TICKET_WIDTH) -> list[str]:
    qty = _qty_display(line.get("cantidad") or "0", width=3)
    product_name = _clean_inline(line.get("producto_nombre") or line.get("producto_id") or "Producto")
    unit_price = _money(line.get("precio_unitario") or "0")
    line_total = _money(line.get("importe_linea") or "0")

    prefix = f"{qty} "
    suffix = unit_price
    available_name = width - len(prefix) - len(suffix) - 1

    if available_name < 1:
        available_name = 1

    name = _truncate_with_ellipsis(product_name, available_name)
    dots = "." * max(1, width - len(prefix) - len(name) - len(suffix))
    return [f"{prefix}{name}{dots}{suffix}", line_total.rjust(width)]


def _ticket_datetime(invoice: dict[str, str]) -> str:
    raw_fecha = (invoice.get("fecha") or "").strip()
    if raw_fecha:
        try:
            dt = datetime.strptime(raw_fecha, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            try:
                dt = datetime.strptime(raw_fecha[:10], "%Y-%m-%d")
                fid = (invoice.get("factura_id") or "").strip()
                if fid.startswith("FV-") and len(fid) >= 17:
                    time_chunk = fid[3:17]
                    created = datetime.strptime(time_chunk, "%Y%m%d%H%M%S")
                    return f"{dt.strftime('%d/%m/%Y')} {created.strftime('%H:%M')}"
                return dt.strftime("%d/%m/%Y 00:00")
            except ValueError:
                pass

    fid = (invoice.get("factura_id") or "").strip()
    if fid.startswith("FV-") and len(fid) >= 17:
        try:
            dt = datetime.strptime(fid[3:17], "%Y%m%d%H%M%S")
            return dt.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            pass
    return datetime.now().strftime("%d/%m/%Y %H:%M")


def format_invoice_ticket(
    invoice: dict[str, str],
    lines: list[dict[str, str]],
    width: int = TICKET_WIDTH,
    title: str = "BUEN YANTAR",
) -> str:
    ticket_lines: list[str] = [
        _center(title, width),
        _center("FACTURA", width),
        _rule("-", width),
    ]

    ticket_lines.extend(_format_label_value("Ref", invoice.get("factura_id") or "-", width))
    ticket_lines.extend(_format_label_value("Fecha", _ticket_datetime(invoice)[:10], width))

    cliente = (invoice.get("cliente") or "").strip()
    if cliente:
        ticket_lines.extend(_format_label_value("Cliente", cliente, width))

    usuario = (invoice.get("usuario") or "").strip()
    if usuario:
        ticket_lines.extend(_format_label_value("Usuario", usuario, width))

    nota_global = (invoice.get("nota_global") or "").strip()
    if nota_global:
        ticket_lines.extend(_format_note_block(nota_global, width))

    ticket_lines.append(_rule("-", width))

    for index, line in enumerate(lines):
        ticket_lines.extend(_format_product_ticket_block(line, width))

        note = (line.get("nota") or "").strip()
        if note:
            ticket_lines.extend(_format_note_block(note, width))
        if index != len(lines) - 1:
            ticket_lines.append("")

    ticket_lines.extend([
        _rule("-", width),
        "",
        f"TOTAL {_money(invoice.get('total_importe') or '0')}".rjust(width),
        "",
    ])

    return RAW_NEWLINE.join(ticket_lines)


def _get_default_printer_name() -> str:
    if os.name != "nt":
        raise RuntimeError("La impresion directa solo esta soportada en Windows.")

    winspool = ctypes.WinDLL("winspool.drv")
    needed = ctypes.c_uint(0)
    winspool.GetDefaultPrinterW(None, ctypes.byref(needed))
    if needed.value <= 1:
        raise RuntimeError("No hay una impresora predeterminada configurada.")

    buffer = ctypes.create_unicode_buffer(needed.value)
    if not winspool.GetDefaultPrinterW(buffer, ctypes.byref(needed)):
        raise ctypes.WinError()
    return buffer.value


class DOCINFO1W(ctypes.Structure):
    _fields_ = [
        ("pDocName", ctypes.c_wchar_p),
        ("pOutputFile", ctypes.c_wchar_p),
        ("pDatatype", ctypes.c_wchar_p),
    ]


def _send_raw_to_default_printer(ticket_text: str) -> str:
    printer_name = _get_default_printer_name()
    winspool = ctypes.WinDLL("winspool.drv")

    handle = ctypes.c_void_p()
    if not winspool.OpenPrinterW(ctypes.c_wchar_p(printer_name), ctypes.byref(handle), None):
        raise ctypes.WinError()

    doc_info = DOCINFO1W("Buen Yantar Ticket", None, "RAW")
    data = ticket_text.encode("cp850", errors="replace")
    written = ctypes.c_uint(0)

    try:
        job_id = winspool.StartDocPrinterW(handle, 1, ctypes.byref(doc_info))
        if job_id == 0:
            raise ctypes.WinError()

        if winspool.StartPagePrinter(handle) == 0:
            raise ctypes.WinError()
        try:
            if winspool.WritePrinter(handle, data, len(data), ctypes.byref(written)) == 0:
                raise ctypes.WinError()
            if written.value != len(data):
                raise RuntimeError("La impresora no acepto todos los bytes del ticket.")
        finally:
            if winspool.EndPagePrinter(handle) == 0:
                raise ctypes.WinError()

        if winspool.EndDocPrinter(handle) == 0:
            raise ctypes.WinError()
    finally:
        winspool.ClosePrinter(handle)

    return printer_name


def print_text_ticket(ticket_text: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = output_dir / f"ticket_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}.txt"
    ticket_path.write_text(ticket_text, encoding="utf-8")

    try:
        _send_raw_to_default_printer(ticket_text)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"No se pudo enviar el ticket a la impresora predeterminada: {exc}") from exc

    return ticket_path
