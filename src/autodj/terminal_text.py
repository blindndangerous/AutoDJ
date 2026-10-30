"""Small helpers for readable terminal prose."""

from __future__ import annotations

import shutil
import textwrap


def format_text(
    message: str,
    *,
    width: int | None = None,
    initial_indent: str = "",
    subsequent_indent: str = "",
) -> str:
    """Wrap prose to the terminal width while preserving explicit line breaks."""
    if width is None:
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
    width = max(1, width)
    wrapped_lines: list[str] = []
    for line in message.split("\n"):
        if not line:
            wrapped_lines.append("")
            continue
        wrapped_lines.extend(
            textwrap.wrap(
                line,
                width=width,
                initial_indent=initial_indent,
                subsequent_indent=subsequent_indent,
                break_long_words=False,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=True,
            )
            or [initial_indent.rstrip()]
        )
    return "\n".join(wrapped_lines)
