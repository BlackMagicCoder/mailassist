"""Small text helpers without dependencies on other mailassist modules."""

from __future__ import annotations

import re
from email.utils import getaddresses

TODO_PATTERN = re.compile(r"\[\[TODO:[^\]]*\]\]")


def count_todos(text: str) -> int:
    return len(TODO_PATTERN.findall(text))


def addresses(header: str) -> set[str]:
    """Bare, lower-cased addresses from a header value; display names are ignored."""
    return {addr.lower() for _, addr in getaddresses([header]) if "@" in addr}
