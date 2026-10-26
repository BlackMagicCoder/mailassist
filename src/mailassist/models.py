"""Domain models shared by connectors, process logic and front ends."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

from .prompt_utils import count_todos


@dataclass(frozen=True)
class MessageRef:
    folder: str
    uid: int
    uidvalidity: int

    def as_args(self) -> dict[str, Any]:
        return {"folder": self.folder, "uid": self.uid, "uidvalidity": self.uidvalidity}


@dataclass(frozen=True)
class EmailSummary:
    ref: MessageRef
    subject: str
    sender: str
    date: str
    message_id: str = ""


@dataclass(frozen=True)
class Email:
    ref: MessageRef
    subject: str
    sender: str
    to: list[str]
    date: str
    body: str
    truncated: bool = False
    message_id: str = ""
    # What the connector removed before the text could reach the model (e.g. hidden HTML).
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThreadMessage:
    uid: int
    subject: str
    sender: str
    date: str
    body: str
    truncated: bool = False
    message_id: str = ""


@dataclass(frozen=True)
class SavedDraft:
    subject: str
    sender: str
    to: list[str]
    cc: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DraftProposal:
    """A reply draft awaiting the owner's decision. `id` and `content_hash` bind an
    approval to exactly this text for exactly this message."""

    ref: MessageRef
    subject: str
    recipient: str
    body: str
    model: str
    message_id: str = ""
    warnings: tuple[str, ...] = ()
    # Unbacked business commitments: saving needs an extra, hash-bound confirmation.
    critical: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: secrets.token_hex(6))

    @property
    def content_hash(self) -> str:
        material = f"{self.ref.folder}\x00{self.ref.uid}\x00{self.ref.uidvalidity}\x00{self.body}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def reply_subject(self) -> str:
        return self.subject if self.subject.lower().startswith("re:") else f"Re: {self.subject}"

    @property
    def todo_count(self) -> int:
        return count_todos(self.body)
