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


# --- business commitments ---------------------------------------------------------
#
# Word-level promises ("kostenlos", "wir erstatten") carry no number the check above
# could catch. Each draft clause is matched against a small keyword list per kind of
# commitment and then compared with clauses of the same kind in the owner's texts or
# approved facts - never in the mail: a customer claiming a promise is no evidence.
# Keyword rules, not semantics: quotes, questions and negated clauses are skipped,
# everything else that is not clearly backed is reported.

COMMITMENTS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "Kostenfreiheit": r"kostenlos|kostenfrei|gratis|umsonst|unentgeltlich|ohne\s+(?:Berechnung|Aufpreis|Gebühr|(?:zusätzliche\s+)?Kosten)"
                          r"|frei\s+Haus|(?:versand|liefer)\w*\b[^,;]*?\bfrei\b|free\s+of\s+charge|for\s+free|at\s+no\s+(?:extra\s+|additional\s+)?(?:cost|charge)",
        "Rabatt": r"\w*rabatt|\w*nachlass|skonto|sonderpreis|sonderkondition|discount",
        "Erstattung": r"\w*erstatt|zurückzahl|refund|reimburs|money\s+back",
        "Gutschrift/Kulanz": r"gutschrift|gutschreib|gutgeschrieben|kulanz|credit\s+note",
        "Garantie": r"\w*garanti|guarante",
        "Liefer-/Terminzusage": r"(?<!un)verbindlich\w*\s+(?:zu|bestätig|fest)|verbindlich\w*\s+\w*(?:liefer|termin)"
                                r"|(?:liefer|termin)\w*[^,;]*?(?<!un)verbindlich|zusag|zugesagt|zusicher|zugesichert|spätestens|binding|\bpromise"
                                r"|(?:liefer|versand|versend|verschick|zustell|\bdeliver|\bship)\w*\b[^,;]*?\b(?:heute|morgen|übermorgen"
                                r"|montag|dienstag|mittwoch|donnerstag|freitag|samstag|innerhalb|bis\s+(?:zum|ende|\d)|am\s+\d"
                                r"|\d+\s*(?:bis\s*\d+\s*)?(?:werk)?(?:tag|woche)|today|tomorrow|within|by\s+\w+day)",
    }.items()
}
_NEGATION = re.compile(r"\b(?:nicht(?!\s+nur)|kein\w*|weder|keinesfalls|ausgeschlossen|not|no|cannot|can't|unable|never)\b", re.IGNORECASE)
_QUOTED = re.compile(r"„[^“”\n]*[“”]|“[^”\n]*”|\"[^\"\n]*\"|»[^«\n]*«|^>.*$", re.MULTILINE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r"[,;]|\s(?:aber|jedoch|sondern|und|but|and)\s", re.IGNORECASE)
_CODE = re.compile(r"\b[A-Za-z]+[-‑]\d+\b")  # product codes like FB-200 are no figures
_TIME_WORD = re.compile(r"\b(?:heute|morgen|übermorgen|montag|dienstag|mittwoch|donnerstag|freitag|samstag|today|tomorrow)\b",
                        re.IGNORECASE)


def _clauses(text: str) -> list[tuple[str, str]]:
    """(clause, its sentence) for clauses that state something: no quotes, questions or negations.
    Negations count per clause, conditions ("…, da der Warenwert über 500 € liegt") per sentence."""
    text = _QUOTED.sub(" ", TODO_PATTERN.sub(" ", text))
    out = []
    for sentence in _SENTENCE.split(text):
        if sentence.rstrip().endswith("?"):
            continue
        out += [(c.strip(" -*•\t"), sentence) for c in _CLAUSE.split(sentence)
                if c.strip(" -*•\t") and not _NEGATION.search(c)]
    return out


def _figures(clause: str, ignore: set[str] = frozenset()) -> set[str]:
    """Percentages, other numbers and day words of a clause, comparable across texts."""
    clause = _CODE.sub(" ", clause)
    found: set[str] = set()
    for token in PERCENT.findall(clause):
        found |= {"%" + key for key in _number_keys(token)}
    for token in NUMBER.findall(PERCENT.sub(" ", clause)):
        keys = _number_keys(token)
        if not keys & ignore:
            found |= keys
    return found | {w.casefold() for w in _TIME_WORD.findall(clause)}


def commitment_warnings(draft: str, owner_texts: list[str], facts: str, mail: list[str]) -> tuple[list[str], list[str]]:
    """(critical, hints) for business commitments in the draft.

    Critical: no clause of that kind in the owner's instruction or the approved facts,
    or a figure (percent, number, day) that none of them contains. Hints: the kind is
    approved, but the draft's wording cannot be matched exactly - the approved text is
    quoted for comparison. Silent only if an approved clause states the same figures.
    Plain numbers found in the mail or the approved texts (quantities, list prices) are no
    deviation - the number check covers them; percentages and day words are."""
    facts = re.sub(r"^## .*$", " ", facts, flags=re.MULTILINE)
    plain: set[str] = set()
    for token in NUMBER.findall(PERCENT.sub(" ", "\n".join([*mail, *owner_texts, facts]))):
        plain |= _number_keys(token)
    owner = [c for c, _ in _clauses("\n".join(t for t in owner_texts if t))]
    approved = [c for c, _ in _clauses(facts)]

    critical: list[str] = []
    hints: list[str] = []
    for clause, sentence in _clauses(draft):
        for kind, pattern in COMMITMENTS.items():
            if not pattern.search(clause):
                continue
            stated, unexplained = _figures(sentence), _figures(clause, plain)
            by_owner = [_figures(c) for c in owner if pattern.search(c)]
            by_facts = [(c, _figures(c)) for c in approved if pattern.search(c)]
            known = set().union(*by_owner, *(f for _, f in by_facts))
            words = set(re.findall(r"\w{4,}", clause.casefold()))
            closest = sorted(by_facts, key=lambda cf: -len(words & set(re.findall(r"\w{4,}", cf[0].casefold()))))
            approved_text = " / ".join(f"„{c}“" for c, _ in closest[:2])
            if not by_owner and not by_facts:
                critical.append(f"Zusage ohne Freigabe ({kind}): „{clause}“ – weder deine Anweisung noch die "
                                "freigegebenen Firmeninfos enthalten so etwas.")
            elif not unexplained <= known:
                extra = ", ".join(f"{k[1:]} %" if k.startswith("%") else k for k in sorted(unexplained - known))
                critical.append(f"Zusage weicht ab ({kind}): „{clause}“ – {extra} steht so in keiner Freigabe.")
            elif not by_owner and all(f for _, f in by_facts) and not any(f & stated for _, f in by_facts):
                # e.g. "Lieferung kostenlos" where only "Versand frei ab 500 €" is approved
                critical.append(f"Zusage ohne die freigegebene Bedingung ({kind}): „{clause}“ – freigegeben ist nur: "
                                f"{approved_text}")
            elif not by_owner and not any(f and f <= stated and unexplained <= f for _, f in by_facts):
                hints.append(f"Zusage prüfen ({kind}): „{clause}“ – freigegeben ist nur: {approved_text}")
            else:
                continue
            break  # one finding per clause is enough for the reviewer
    return list(dict.fromkeys(critical)), list(dict.fromkeys(hints))
