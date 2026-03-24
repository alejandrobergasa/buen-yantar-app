from __future__ import annotations

from textwrap import wrap


PDF_PAGE_WIDTH = 595
PDF_PAGE_HEIGHT = 842
PDF_MARGIN_X = 40
PDF_START_Y = 800
PDF_FONT_SIZE = 10
PDF_LEADING = 13
PDF_MAX_LINE_WIDTH = 88
PDF_MAX_LINES_PER_PAGE = 56


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _normalize_pdf_text(text: str) -> str:
    return text.encode("cp1252", "replace").decode("cp1252")


def _wrap_markdown_line(line: str) -> list[str]:
    normalized = _normalize_pdf_text(line.rstrip())
    if not normalized:
        return [""]
    if len(normalized) <= PDF_MAX_LINE_WIDTH:
        return [normalized]

    if normalized.startswith("- "):
        wrapped = wrap(normalized[2:], width=PDF_MAX_LINE_WIDTH - 2, break_long_words=True, break_on_hyphens=False)
        return [f"- {wrapped[0]}"] + [f"  {item}" for item in wrapped[1:]]

    return wrap(normalized, width=PDF_MAX_LINE_WIDTH, break_long_words=True, break_on_hyphens=False)


def _paginate_lines(lines: list[str]) -> list[list[str]]:
    pages: list[list[str]] = []
    current_page: list[str] = []

    for line in lines:
        if len(current_page) >= PDF_MAX_LINES_PER_PAGE:
            pages.append(current_page)
            current_page = []
        current_page.append(line)

    if current_page or not pages:
        pages.append(current_page)

    return pages


def _build_content_stream(lines: list[str]) -> bytes:
    chunks: list[bytes] = [
        b"BT\n",
        f"/F1 {PDF_FONT_SIZE} Tf\n".encode("ascii"),
        f"{PDF_MARGIN_X} {PDF_START_Y} Td\n".encode("ascii"),
        f"{PDF_LEADING} TL\n".encode("ascii"),
    ]

    first_line = True
    for line in lines:
        escaped = _escape_pdf_text(line)
        encoded = f"({escaped}) Tj\n".encode("cp1252")
        if first_line:
            chunks.append(encoded)
            first_line = False
        else:
            chunks.append(b"T* ")
            chunks.append(encoded)

    chunks.append(b"ET")
    return b"".join(chunks)


def build_markdown_pdf(title: str, markdown_text: str) -> bytes:
    raw_lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    wrapped_lines: list[str] = []
    for line in raw_lines:
        wrapped_lines.extend(_wrap_markdown_line(line))

    pages = _paginate_lines(wrapped_lines)
    font_object_id = 3
    page_count = len(pages)

    objects: list[bytes] = []

    page_object_ids = [4 + index * 2 for index in range(page_count)]
    content_object_ids = [5 + index * 2 for index in range(page_count)]

    kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Count {page_count} /Kids [{kids}] >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    for page_id, content_id, page_lines in zip(page_object_ids, content_object_ids, pages):
        page_object = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PDF_PAGE_WIDTH} {PDF_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        stream = _build_content_stream(page_lines)
        content_object = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") +
            stream +
            b"\nendstream"
        )
        objects.append(page_object)
        objects.append(content_object)

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    output = bytearray(header)
    offsets = [0]

    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    normalized_title = _normalize_pdf_text(title)
    output.extend(
        (
            "trailer\n"
            f"<< /Size {len(offsets)} /Root 1 0 R /Info << /Title ({_escape_pdf_text(normalized_title)}) >> >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("cp1252")
    )

    return bytes(output)


__all__ = ["build_markdown_pdf"]
