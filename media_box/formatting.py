import re


def truncate(text: str, width: int = 40) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def format_size(size_bytes: int | float | None) -> str:
    if size_bytes is None or size_bytes < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def format_progress(fraction: float) -> str:
    pct = fraction * 100
    filled = int(pct // 5)
    bar = "█" * filled + "░" * (20 - filled)
    return f"{bar} {pct:5.1f}%"


def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def format_table(rows: list[dict], columns: list[tuple[str, str, int]]) -> str:
    """Return a formatted table as a string.

    columns: list of (header, dict_key, width).  A width of 0 means auto-size
    from the data (no truncation).
    """
    if not rows:
        return "  (no results)"

    lines: list[str] = []

    # Resolve auto-width (0) columns from data
    widths = []
    for header, key, width in columns:
        if width == 0:
            width = max(len(header), max(len(str(row.get(key, ""))) for row in rows))
        widths.append(width)

    # Header
    header_parts = []
    for (header, _, _), w in zip(columns, widths):
        header_parts.append(header.ljust(w))
    header_line = "  ".join(header_parts)
    lines.append(header_line)
    lines.append("─" * len(header_line))

    # Rows
    for row in rows:
        parts = []
        for (_, key, _), w in zip(columns, widths):
            val = str(row.get(key, ""))
            parts.append(truncate(val, w).ljust(w))
        lines.append("  ".join(parts))

    return "\n".join(lines)


def print_table(rows: list[dict], columns: list[tuple[str, str, int]]) -> None:
    """Print a formatted table.

    columns: list of (header, dict_key, width).  A width of 0 means auto-size
    from the data (no truncation).
    """
    if not rows:
        print("  (no results)")
        return

    # Resolve auto-width (0) columns from data
    widths = []
    for header, key, width in columns:
        if width == 0:
            width = max(len(header), max(len(str(row.get(key, ""))) for row in rows))
        widths.append(width)

    # Header
    header_parts = []
    for (header, _, _), w in zip(columns, widths):
        header_parts.append(header.ljust(w))
    header_line = "  ".join(header_parts)
    print(header_line)
    print("─" * len(header_line))

    # Rows
    for row in rows:
        parts = []
        for (_, key, _), w in zip(columns, widths):
            val = str(row.get(key, ""))
            parts.append(truncate(val, w).ljust(w))
        print("  ".join(parts))
