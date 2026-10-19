"""Load the synthetic corpus (src/mailassist/demo_data/beispiel_gmbh.toml) as domain objects."""

from __future__ import annotations

import tomllib
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from mailassist.mail import html_to_text
from mailassist.models import Email, MessageRef, ThreadMessage

CORPUS = Path(__file__).parents[1] / "src" / "mailassist" / "demo_data" / "beispiel_gmbh.toml"
OWN = frozenset({"vertrieb@beispiel-maschinenbau.example"})


@dataclass
class Case:
    id: str
    lang: str
    category: str
    sender: str
    subject: str
    body: str
    known: list[str]
    unknown: list[str]
    injection: bool = False
    attacker_markers: list[str] = field(default_factory=list)
    thread: list[dict] = field(default_factory=list)
    raw_html: str | None = None
    hidden_text: str | None = None

    def email(self) -> Email:
        ref = MessageRef("All Mail", 1000 + zlib.crc32(self.id.encode()) % 1000, 1)
        return Email(ref, self.subject, self.sender, ["vertrieb@beispiel-maschinenbau.example"],
                     "2026-10-19T08:00:00Z", self.body, message_id=f"{self.id}@corpus.example")

    def thread_messages(self) -> list[ThreadMessage]:
        earlier = [ThreadMessage(i + 1, f"Re: {self.subject}", t["from"], "2026-10-18T08:00:00Z", t["body"],
                                 message_id=f"{self.id}-t{i}@corpus.example") for i, t in enumerate(self.thread)]
        selected = ThreadMessage(99, self.subject, self.sender, "2026-10-19T08:00:00Z", self.body,
                                 message_id=f"{self.id}@corpus.example")
        return earlier + [selected]


def load_cases() -> list[Case]:
    data = tomllib.loads(CORPUS.read_text(encoding="utf-8"))
    cases = []
    for m in data["mail"]:
        html_body = m.get("body_html")
        cases.append(Case(
            id=m["id"], lang=m["lang"], category=m["category"], sender=m["from"], subject=m["subject"],
            body=html_to_text(html_body) if html_body else m["body"], known=m["known"], unknown=m["unknown"],
            injection=m.get("injection", False), attacker_markers=m.get("attacker_markers", []),
            thread=m.get("thread", []), raw_html=html_body, hidden_text=m.get("hidden_text"),
        ))
    return cases
