"""Approved company information ("Wissensbasis") for grounded drafts.

One Markdown file per topic in the knowledge directory, with front matter:

    ---
    title: Lieferbedingungen
    approved_by: Vertrieb (K. Muster)
    approved_on: 2026-10-01
    valid_until: 2026-12-31      # optional
    ---
    Text the assistant may rely on.

Only files with `approved_by` and a valid `approved_on` that is not in the future
and not past `valid_until` are used. Everything else is skipped with a reason.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

MAX_FACTS_CHARS = 8000


@dataclass(frozen=True)
class KnowledgeEntry:
    name: str
    title: str
    body: str
    approved_by: str
    approved_on: date
    valid_until: date | None = None


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for end, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            meta = {}
            for entry in lines[1:end]:
                key, sep, value = entry.partition(":")
                if sep:
                    meta[key.strip()] = value.split("#", 1)[0].strip()
            return meta, "\n".join(lines[end + 1 :])
    return {}, text


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def load_knowledge(directory: str | Path, today: date | None = None) -> tuple[list[KnowledgeEntry], list[str]]:
    """Valid entries and human-readable reasons for skipped files."""
    today = today or date.today()
    folder = Path(directory)
    if not folder.is_dir():
        return [], []
    entries, skipped = [], []
    for file in sorted(folder.glob("*.md")):
        meta, body = _front_matter(file.read_text(encoding="utf-8"))
        approved_on, valid_until = _date(meta.get("approved_on")), _date(meta.get("valid_until"))
        if not meta.get("approved_by") or approved_on is None:
            skipped.append(f"{file.name}: keine Freigabe (approved_by/approved_on fehlt oder ungültig)")
        elif approved_on > today:
            skipped.append(f"{file.name}: Freigabedatum liegt in der Zukunft")
        elif meta.get("valid_until") and valid_until is None:
            skipped.append(f"{file.name}: valid_until ungültig")
        elif valid_until is not None and valid_until < today:
            skipped.append(f"{file.name}: abgelaufen am {valid_until.isoformat()}")
        elif not body.strip():
            skipped.append(f"{file.name}: leer")
        else:
            entries.append(KnowledgeEntry(
                name=file.name, title=meta.get("title") or file.stem, body=body.strip(),
                approved_by=meta["approved_by"], approved_on=approved_on, valid_until=valid_until,
            ))
    return entries, skipped


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w{4,}", text.casefold())}


def select_facts(entries: list[KnowledgeEntry], query: str, max_chars: int = MAX_FACTS_CHARS) -> list[KnowledgeEntry]:
    """Most relevant entries first (word overlap with the mail), within a size budget."""
    wanted = _words(query)
    ranked = sorted(entries, key=lambda e: (-len(wanted & _words(e.title + " " + e.body)), e.name))
    chosen, used = [], 0
    for entry in ranked:
        size = len(entry.title) + len(entry.body) + 80
        if used + size > max_chars:
            continue
        chosen.append(entry)
        used += size
    return chosen


def facts_block(entries: list[KnowledgeEntry]) -> str:
    return "\n\n".join(
        f"## {e.title} (freigegeben von {e.approved_by} am {e.approved_on.isoformat()})\n{e.body}" for e in entries
    )
