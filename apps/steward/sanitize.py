from __future__ import annotations

import unicodedata


def safe_text(value: str, limit: int) -> str:
    without_controls = "".join(character for character in value if unicodedata.category(character) not in {"Cc", "Cf"})
    normalized = " ".join(without_controls.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"
