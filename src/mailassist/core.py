"""Workflow shared by the CLI and the Telegram bot: search, propose a draft, save it.

Nothing here can send mail. Saving a draft is a separate step that passes the
approval gate: registered proposal, same owner, matching content hash, once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from .approval import ApprovalGate
from .config import Settings
from .llm import NanoGPTClient
from .mail import BaryonMail, MailBackend, MailError
from .models import DraftProposal, EmailSummary, MessageRef, SavedDraft
from .prompt import build_user_prompt, count_todos, system_prompt
from .triage import Triage, analyse as analyse_mail

__all__ = ["Assistant", "DraftProposal", "MailSession"]

log = logging.getLogger(__name__)

MailSession = Callable[[], AbstractAsyncContextManager[MailBackend]]

SAVE_FAILED = (
    "Speichern fehlgeschlagen. Der Entwurf könnte trotzdem angelegt worden sein – "
    "bitte zuerst im Entwürfe-Ordner von Proton Mail nachsehen, bevor du erneut speicherst."
)


class Assistant:
    def __init__(
        self,
        settings: Settings,
        mail_session: MailSession,
        llm: NanoGPTClient,
        gate: ApprovalGate | None = None,
    ):
        self._settings = settings
        self._mail_session = mail_session
        self._llm = llm
        self._gate = gate or ApprovalGate()

    @classmethod
    def from_settings(cls, settings: Settings) -> Assistant:
        llm = NanoGPTClient(
            api_key=settings.nanogpt_api_key,
            model=settings.model,
            base_url=settings.nanogpt_base_url,
            max_tokens=settings.max_tokens,
        )
        return cls(settings, lambda: BaryonMail(settings.baryon_command, thread_folder=settings.search_folder), llm)

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
        owner: str = "cli",
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
        proposal = DraftProposal(
            ref=ref,
            subject=email.subject,
            recipient=email.sender,
            body=completion.text,
            model=completion.model,
            message_id=email.message_id,
        )
        self._gate.register(proposal, owner)
        log.info("proposal id=%s uid=%s thread=%d todos=%d", proposal.id, ref.uid, len(thread), count_todos(proposal.body))
        return proposal

    async def analyse(self, ref: MessageRef) -> tuple[Triage, str]:
        """Classify and extract; read-only, nothing is stored or executed."""
        async with self._mail_session() as mail:
            email = await mail.get_email(ref)
            thread = await mail.get_thread(ref)
        triage, model = await analyse_mail(self._llm, email, thread, self._settings.own_addresses)
        facts = [f for f in triage.facts() if f.status != "unknown"]
        log.info(
            "triage uid=%s category=%s facts=%d verified=%d injection=%s",
            ref.uid, triage.category, len(facts), sum(f.verified for f in facts), triage.injection_suspected,
        )
        return triage, model

    def current_proposal(self, proposal_id: str, owner: str) -> DraftProposal:
        return self._gate.current(proposal_id, owner)

    def discard(self, proposal_id: str, owner: str) -> None:
        self._gate.discard(proposal_id, owner)
        log.info("proposal id=%s discarded", proposal_id)

    async def save(self, proposal: DraftProposal, owner: str = "cli") -> SavedDraft:
        return await self.save_approved(proposal.id, proposal.content_hash, owner)

    async def save_approved(self, proposal_id: str, content_hash: str, owner: str) -> SavedDraft:
        proposal = self._gate.begin(proposal_id, content_hash, owner)
        try:
            async with self._mail_session() as mail:
                saved = await mail.save_reply_draft(proposal.ref, proposal.body)
        except MailError as exc:
            self._gate.finish(proposal_id, ok=False)
            raise MailError(SAVE_FAILED, detail=exc.detail or str(exc)) from None
        except BaseException:
            self._gate.finish(proposal_id, ok=False)
            raise
        self._gate.finish(proposal_id, ok=True)
        log.info("proposal id=%s saved", proposal_id)
        return saved
