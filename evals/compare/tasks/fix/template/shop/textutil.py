"""Plain text helpers for reports and exports."""

import re


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def pad_right(text, width):
    text = str(text)
    return text + " " * max(0, width - len(text))


def pad_left(text, width):
    text = str(text)
    return " " * max(0, width - len(text)) + text


def render_table(header, rows, right_align=()):
    """Render rows as an aligned text table. right_align holds column indexes."""
    table = [list(map(str, header))] + [list(map(str, r)) for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    lines = []
    for n, row in enumerate(table):
        cells = [pad_left(c, widths[i]) if i in right_align else pad_right(c, widths[i]) for i, c in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def truncate(text, width):
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"
