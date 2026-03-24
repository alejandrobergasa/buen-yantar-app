from __future__ import annotations

from secrets import choice
from string import ascii_uppercase, digits


ID_LENGTH = 10
ID_ALPHABET = ascii_uppercase + digits


def short_id(length: int = ID_LENGTH) -> str:
    return "".join(choice(ID_ALPHABET) for _ in range(max(1, length)))


def prefixed_id(prefix: str, total_length: int = ID_LENGTH) -> str:
    clean_prefix = (prefix or "").strip().upper()
    if not clean_prefix:
        return short_id(total_length)

    prefix_text = f"{clean_prefix}-"
    suffix_length = max(1, total_length - len(prefix_text))
    return f"{prefix_text}{short_id(suffix_length)}"
