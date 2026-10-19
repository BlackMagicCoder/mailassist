"""Deterministic output checks for drafts.

Every number, date, time, URL and email address in a draft must be backed by
one of the sources the owner can vouch for: the mail, the thread, the owner's
instruction, approved company facts or the signature. Unbacked items become
warnings shown before approval - a hint for the human, not a hard block.
"""

from __future__ import annotations

import re
from datetime import datetime

from .prompt_utils import TODO_PATTERN

URL = re.compile(r"(?:https?://|www\.)[^\s<>()\"'„“]+", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
NUMBER = re.compile(r"(?<![\w])\d+(?:[.,:/]\d+)*")
LIST_MARKER = re.compile(r"^\s*\d{1,2}[.)]\s", re.MULTILINE)
PERCENT = re.compile(r"(?<![\w])(\d+(?:[.,]\d+)?)\s?(?:%|prozent|percent)", re.IGNORECASE)


def _number_keys(token: str) -> set[str]:
    digits = re.sub(r"\D", "", token).lstrip("0") or "0"
    keys = {digits}
    if re.search(r"[.,:]00$", token):  # 1.250,00 ~ 1.250 ; 14:00 ~ 14
        keys.add(re.sub(r"\D", "", token[:-3]).lstrip("0") or "0")
    return keys


def date_variants(iso: str) -> str:
    """German spellings of an ISO timestamp, so "Ihre Mail vom 29.09.2026" is backed by the header date."""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{iso} {d:%d.%m.%Y} {d.day}.{d.month}.{d.year} {d:%d.%m.}"


def unsupported_claims(draft: str, sources: list[str], trusted_sources: list[str] | None = None) -> list[str]:
    """`sources` back numbers and dates (the mail may legitimately contain an order number).
    Links and email addresses must come from `trusted_sources` (owner instruction, company
    facts, signature): one copied from the incoming mail is how injected links get into replies."""
    corpus = "\n".join(s for s in sources if s)
    trusted = "\n".join(s for s in (trusted_sources if trusted_sources is not None else sources) if s).casefold()
    text = LIST_MARKER.sub(" ", TODO_PATTERN.sub(" ", draft))

    findings: list[str] = []
    for url in URL.findall(text):
        url = url.rstrip(".,;:!?")
        if url.casefold() not in trusted:
            findings.append(url)
    text = URL.sub(" ", text)
    for address in EMAIL.findall(text):
        if address.casefold() not in trusted:
            findings.append(address)
    text = EMAIL.sub(" ", text)

    # Percentages (discounts, surcharges) are company commitments: only trusted sources back them.
    trusted_percent: set[str] = set()
    for token in PERCENT.findall(trusted):
        trusted_percent |= _number_keys(token)
    for token in PERCENT.findall(text):
        if not _number_keys(token) & trusted_percent:
            findings.append(f"{token} %")
    text = PERCENT.sub(" ", text)

    known: set[str] = set()
    for token in NUMBER.findall(EMAIL.sub(" ", URL.sub(" ", corpus))):
        known |= _number_keys(token)
    for token in NUMBER.findall(text):
        if not _number_keys(token) & known:
            findings.append(token)

    unique = list(dict.fromkeys(findings))
    return [f"Bitte prüfen: „{item}“ ist durch Mail, Verlauf, Anweisung oder Firmeninfos nicht belegt." for item in unique]
