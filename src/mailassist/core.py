"""Workflow shared by the CLI and the Telegram bot: search, propose a draft, save it.

Nothing here can send mail. Saving a draft is always a separate, explicit step.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from .config import Settings
from .llm import NanoGPTClient
from .mail import BaryonMail, EmailSummary, MailBackend, MessageRef, SavedDraft
from .prompt import build_user_prompt, count_todos, system_prompt

log = logging.getLogger(__name__)

MailSession = Callable[[], AbstractAsyncContextManager[MailBackend]]


@dataclass(frozen=True)
class DraftProposal:
    ref: MessageRef
    subject: str
    recipient: str
    body: str
    model: str

    @property
    def reply_subject(self) -> str:
        return self.subject if self.subject.lower().startswith("re:") else f"Re: {self.subject}"

    @property
    def todo_count(self) -> int:
        return count_todos(self.body)


class Assistant:
    def __init__(self, settings: Settings, mail_session: MailSession, llm: NanoGPTClient):
        self._settings = settings
        self._mail_session = mail_session
        self._llm = llm

    @classmethod
    def from_settings(cls, settings: Settings) -> Assistant:
        llm = NanoGPTClient(
            api_key=settings.nanogpt_api_key,
            model=settings.model,
            base_url=settings.nanogpt_base_url,
            max_tokens=settings.max_tokens,
        )
        return cls(settings, lambda: BaryonMail(settings.baryon_command), llm)

    async def recent(self, limit: int = 10) -> list[EmailSummary]:
        async with self._mail_session() as mail:
            return await mail.search(self._settings.inbox_folder, None, limit)

    async def search(self, query: str, limit: int = 10) -> list[EmailSummary]:
        async with self._mail_session() as mail:
            return await mail.search(self._settings.search_folder, query, limit)

    async def propose(
        self,
        ref: MessageRef,
        instruction: str | None = None,
        previous: DraftProposal | None = None,
        change_request: str | None = None,
    ) -> DraftProposal:
        async with self._mail_session() as mail:
            email = await mail.get_email(ref)
            thread = await mail.get_thread(ref)
        user = build_user_prompt(
            email,
            thread,
            instruction,
            own_addresses=self._settings.own_addresses,
            previous_draft=previous.body if previous else None,
            change_request=change_request,
        )
        completion = await self._llm.complete(system_prompt(self._settings.signature), user)
        log.info("proposal uid=%s thread=%d todos=%d", ref.uid, len(thread), count_todos(completion.text))
        return DraftProposal(
            ref=ref, subject=email.subject, recipient=email.sender, body=completion.text, model=completion.model
        )

    async def save(self, proposal: DraftProposal) -> SavedDraft:
        async with self._mail_session() as mail:
            return await mail.save_reply_draft(proposal.ref, proposal.body)
