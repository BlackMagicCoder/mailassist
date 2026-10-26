"""Demo mode: an in-process mailbox with synthetic mails and a free, rule-based mock model.

Nothing here touches a real mailbox or a paid API. Data: demo_data/beispiel_gmbh.toml
(own synthetic B2B mails incl. injection attempts) and demo_data/gretel_de.json
(Gretel synthetic German emails, Apache 2.0 - see demo_data/NOTICE.md).

MockModel is NOT AI. It answers from simple rules, but it reads exactly the prompt a
real model would receive, so the demo shows the real data flow. In "naiv" mode it
deliberately obeys instructions found in mails, to demonstrate the safety checks.
"""

from __future__ import annotations

import json
import re
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

from .llm import Completion
from .mail import MailError, html_to_text_with_notes
from .models import Email, EmailSummary, MessageRef, SavedDraft, ThreadMessage

DEMO_OWNER = "Vertrieb Beispiel Maschinenbau <vertrieb@beispiel-maschinenbau.example>"
DEMO_OWN_ADDRESSES = "vertrieb@beispiel-maschinenbau.example,service@gretel-demo.example"
DEMO_SIGNATURE = "Viele Grüße\\nVertrieb, Beispiel Maschinenbau GmbH (Demo)"
UIDVALIDITY = 1
FOLDERS = ("INBOX", "Sent", "All Mail")


@dataclass
class _Message:
    conversation: str
    sender: str
    subject: str
    date: str
    body: str = ""
    html: str | None = None
    own: bool = False
    message_id: str = ""
    source: str = ""
    uids: dict[str, int] = field(default_factory=dict)


def _data(name: str) -> str:
    return resources.files("mailassist").joinpath("demo_data", name).read_text(encoding="utf-8")


def load_demo_messages() -> list[_Message]:
    base = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    messages: list[_Message] = []

    def add(conv: str, sender: str, subject: str, body: str = "", html: str | None = None, own: bool = False,
            source: str = "") -> None:
        n = len(messages)
        messages.append(_Message(conv, sender, subject, (base + timedelta(hours=2 * n)).isoformat().replace("+00:00", "Z"),
                                 body, html, own, f"demo-{n}@demo.example", source))

    # Gretel first, own B2B mails last: the business requests (incl. injection attempts) are the newest in INBOX.
    for case in json.loads(_data("gretel_de.json")):
        for earlier in case["thread"]:
            add(case["id"], earlier["from"], f"Re: {case['subject']}", earlier["body"], own=earlier["own"], source="gretel")
        add(case["id"], case["from"], case["subject"], case["body"], source="gretel")
    for case in tomllib.loads(_data("beispiel_gmbh.toml"))["mail"]:
        for earlier in case.get("thread", []):
            add(case["id"], earlier["from"], f"Re: {case['subject']}", earlier["body"], source="beispiel_gmbh")
        add(case["id"], case["from"], case["subject"], case.get("body", ""), case.get("body_html"), source="beispiel_gmbh")

    counters = dict.fromkeys(FOLDERS, 0)
    for message in messages:
        for folder in ("Sent" if message.own else "INBOX", "All Mail"):
            counters[folder] += 1
            message.uids[folder] = counters[folder]
    return messages


class DemoMailbox:
    """MailBackend over the synthetic demo mails. Drafts go to a local JSON file, never anywhere else."""

    def __init__(self, drafts_file: str | Path | None = None):
        self._messages = load_demo_messages()
        self._drafts_file = Path(drafts_file) if drafts_file else None
        self.drafts: list[dict] = []

    @asynccontextmanager
    async def session(self):
        yield self

    def _find(self, ref: MessageRef) -> _Message:
        if ref.uidvalidity != UIDVALIDITY:
            raise MailError("Demo-Postfach: veraltete Nachrichten-Referenz.")
        for message in self._messages:
            if message.uids.get(ref.folder) == ref.uid:
                return message
        raise MailError(f"Demo-Postfach: keine Nachricht {ref.uid} in {ref.folder}.")

    async def search(self, folder: str, query: str | None, limit: int = 10) -> list[EmailSummary]:
        found = [m for m in self._messages if folder in m.uids]
        if query:
            needle = query.casefold()
            found = [m for m in found if needle in f"{m.sender}\n{m.subject}\n{m.body}\n{m.html or ''}".casefold()]
        found.sort(key=lambda m: m.date, reverse=True)
        return [EmailSummary(MessageRef(folder, m.uids[folder], UIDVALIDITY), m.subject, m.sender, m.date, m.message_id)
                for m in found[:limit]]

    async def get_email(self, ref: MessageRef) -> Email:
        m = self._find(ref)
        body, notes = html_to_text_with_notes(m.html) if m.html else (m.body, ())
        return Email(ref, m.subject, m.sender, [DEMO_OWNER], m.date, body, message_id=m.message_id, notes=notes)

    async def get_thread(self, ref: MessageRef) -> list[ThreadMessage]:
        selected = self._find(ref)
        thread = sorted((m for m in self._messages if m.conversation == selected.conversation), key=lambda m: m.date)
        out = []
        for m in thread:
            body = html_to_text_with_notes(m.html)[0] if m.html else m.body
            out.append(ThreadMessage(m.uids["All Mail"], m.subject, m.sender, m.date, body[:2000], len(body) > 2000,
                                     m.message_id))
        return out

    async def save_reply_draft(self, ref: MessageRef, body: str) -> SavedDraft:
        m = self._find(ref)
        subject = m.subject if m.subject.lower().startswith("re:") else f"Re: {m.subject}"
        draft = {"saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "to": m.sender, "subject": subject,
                 "in_reply_to": m.message_id, "body": body}
        self.drafts.append(draft)
        if self._drafts_file:
            self._drafts_file.parent.mkdir(parents=True, exist_ok=True)
            existing = json.loads(self._drafts_file.read_text(encoding="utf-8")) if self._drafts_file.exists() else []
            self._drafts_file.write_text(json.dumps(existing + [draft], ensure_ascii=False, indent=1), encoding="utf-8")
        return SavedDraft(subject=subject, sender=DEMO_OWNER, to=[m.sender])


# --- mock model ---------------------------------------------------------------------

def _block(tag: str, text: str) -> str:
    match = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", text, re.DOTALL)
    return match.group(1) if match else ""


def _selected(user: str) -> tuple[str, str, str]:
    block = _block("selected_email", user)
    header, _, body = block.partition("\n\n")
    sender = re.search(r"^From: (.*)$", header, re.MULTILINE)
    subject = re.search(r"^Subject: (.*)$", header, re.MULTILINE)
    return (sender.group(1) if sender else ""), (subject.group(1) if subject else ""), body


_CATEGORY_RULES = [
    ("verdacht_spam_phishing", ("zugangsdaten", "konto wird gesperrt", "/verify", "passwort", "password", "datenklau")),
    ("kuendigung", ("kündig", "cancel my", "cancellation")),
    ("rechnung_zahlung", ("rechnung", "abgebucht", "zahlung", "invoice", "erstatt", "bezahlung")),
    ("lieferstatus", ("wann wird", "wo bleibt", "geliefert?", "delivery status")),
    ("support_reklamation", ("defekt", "ausgefallen", "reklamation", "beschädigt", "damaged", "funktioniert nicht", "steht still")),
    ("bestellung", ("hiermit bestellen", "bestellen wir", "bestellnummer")),
    ("terminanfrage", ("termin", "meeting", " kw ")),
    ("angebotsanfrage", ("angebot", "was kostet", "preis", "quote", "volume discount")),
    ("werbung_newsletter", ("newsletter", "abmelden", "entdecke", "jetzt sichern", "rabattcode", "exklusiv", "gutschein")),
]
_INJECTION = ("ignoriere", "ignore previous", "ki-assistent", "ki:", "ai assistant", "systemprompt", "dekodieren",
              "‹/selected_email", "‹owner_instruction", "an den ki")
_NEXT = {
    "angebotsanfrage": [("angebot_erstellen", "Angebot mit freigegebenen Preisen erstellen", False), ("antworten", "Eingang bestätigen, offene Punkte klären", False)],
    "bestellung": [("aufgabe_anlegen", "Auftrag im Warenwirtschaftssystem anlegen", True), ("antworten", "Bestellung bestätigen", False)],
    "terminanfrage": [("termin_vorschlagen", "Termin im Kalender eintragen", True), ("antworten", "Terminvorschlag beantworten", False)],
    "support_reklamation": [("an_team_weiterleiten", "An Service weiterleiten", True), ("antworten", "Ersteinschätzung zusagen", False)],
    "rechnung_zahlung": [("an_team_weiterleiten", "An Buchhaltung weiterleiten", True), ("antworten", "Prüfung zusagen", False)],
    "lieferstatus": [("antworten", "Lieferstatus mitteilen", False)],
    "kuendigung": [("antworten", "Eingang der Kündigung bestätigen", False), ("aufgabe_anlegen", "Kündigung im Vertragssystem vermerken", True)],
    "info_sonstiges": [("ignorieren", "Zur Kenntnis nehmen", False)],
    "werbung_newsletter": [("ignorieren", "Keine Antwort nötig", False)],
    "verdacht_spam_phishing": [("als_phishing_melden", "Nicht antworten, keine Links öffnen, melden", False)],
}


def _fact(value: str | None) -> dict:
    return {"status": "known", "value": value, "evidence": value} if value else {"status": "unknown"}


def _facts(pattern: str, text: str, limit: int = 5) -> list[dict]:
    return [_fact(m) for m in list(dict.fromkeys(re.findall(pattern, text)))[:limit]]


class MockModel:
    """Rule-based stand-in for the real model. Free, offline, deterministic - and clearly not AI."""

    def __init__(self, naive: bool = False):
        self.naive = naive
        self.name = "mock/regelbasiert" + ("-naiv" if naive else "")

    async def complete(self, system: str, user: str) -> Completion:
        if system.startswith("You analyse"):
            return Completion(json.dumps(self._triage(user), ensure_ascii=False), self.name)
        if system.startswith("Answer with the single word OK"):
            return Completion("OK", self.name)
        return Completion(self._draft(system, user), self.name)

    def _triage(self, user: str) -> dict:
        sender, subject, body = _selected(user)
        text = f"{subject}\n{body}"
        low = f" {text.casefold()} "
        category = next((c for c, words in _CATEGORY_RULES if any(w in low for w in words)), "info_sonstiges")
        injection = [w for w in _INJECTION if w in low]
        name = re.sub(r"\s*<.*?>", "", sender).strip().strip('"')
        english = sum(w in low for w in (" the ", " and ", " please ", " could ", " we ")) >= 2
        company = re.search(r"\b[\w&.-]+(?: [\w&.-]+)? (?:GmbH|AG|KG|SE|Ltd\.?)", text)
        money = re.search(r"\d[\d.]*(?:,\d{2})?\s?(?:€|EUR|Euro)", text)
        questions = [q.strip() for q in re.findall(r"[^.!?\n]{10,200}\?", body)][:3]
        return {
            "category": category, "confidence": 0.5,
            "urgency": "hoch" if any(w in low for w in ("dringend", "sofort", "urgent", "steht still")) else "normal",
            "language": "en" if english else "de",
            "reply_needed": category not in ("werbung_newsletter", "verdacht_spam_phishing", "info_sonstiges"),
            "summary": "Regelbasierte Mock-Einordnung (keine KI): " + (body.strip().split("\n")[0][:160] or subject),
            "company": _fact(company.group(0) if company else None),
            "contact_person": _fact(name if name and " " in name and not re.search(r"\d", name) else None),
            "budget_or_price": _fact(money.group(0) if money else None),
            "reference_numbers": _facts(r"\b[A-Z]{1,4}-\d[\w-]{2,}\b", text),
            "products": _facts(r"\b[A-Z]{2}-\d{2,3}\b", text),
            "quantities": _facts(r"\b\d+ (?:Stück|Sets?|units)\b", text),
            "requested_dates": _facts(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b|\bKW \d{1,2}\b", text),
            "open_questions": questions,
            "injection_suspected": bool(injection),
            "injection_notes": f"Auffällige Stellen: {', '.join(injection)}" if injection else None,
            "next_steps": [{"kind": k, "description": d, "requires_external_change": e} for k, d, e in _NEXT[category]],
        }

    def _draft(self, system: str, user: str) -> str:
        sender, subject, body = _selected(user)
        name = re.sub(r"\s*<.*?>", "", sender).strip().strip('"')
        instruction = _block("owner_instruction", user)
        facts = _block("company_facts", user)
        lines = [f"Guten Tag {name}," if name else "Guten Tag,", "",
                 f"vielen Dank für Ihre Nachricht zu „{subject}“."]
        if instruction and instruction != "Write an appropriate reply.":
            lines.append(f"[[TODO: Antwort im Sinne von „{instruction}“ ausformulieren]]")
        for code in dict.fromkeys(re.findall(r"\b[A-Z]{2}-\d{2,3}\b", body)):
            for fact_line in (line.strip(" -") for line in facts.splitlines()):
                if code in fact_line:
                    lines.append(f"Laut unseren freigegebenen Informationen: {fact_line}")
        for question in re.findall(r"[^.!?\n]{10,200}\?", body)[:3]:
            lines.append(f"Zu Ihrer Frage „{question.strip()}“: [[TODO: beantworten]]")
        if self.naive:  # deliberately obeys the mail - to show that the checks catch it
            for item in dict.fromkeys(re.findall(r"https?://\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\d+\s?%|kostenlos|gratis", body)):
                lines.append(f"Wie in Ihrer Nachricht gewünscht: {item.rstrip('.,')}")
        change = _block("change_request", user)
        if change:
            lines.append(f"[Überarbeitet nach Wunsch „{change}“ – das Mock-Modell vermerkt das nur]")
        signature = re.search(r"exactly this signature:\n(.*?)\n\n?$", system, re.DOTALL)
        lines += ["", signature.group(1).strip() if signature else "Viele Grüße\n[[TODO: Name]]"]
        return "\n".join(lines)
