"""Workflow shared by the CLI and the Telegram bot: search, propose a draft, save it.

Nothing here can send mail. Saving a draft is a separate step that passes the
approval gate: registered proposal, same owner, matching content hash, once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from .approval import ApprovalError, ApprovalGate, DuplicateDraftError
from .audit import AuditLog, DraftLedger, message_key
from .checks import date_variants, unsupported_claims
from .config import Settings
from .knowledge import facts_block, load_knowledge, select_facts
from .llm import Completion, LLMAdapter, NanoGPTClient
from .mail import BaryonMail, MailBackend, MailError
from .models import DraftProposal, Email, EmailSummary, MessageRef, SavedDraft, ThreadMessage
from .prompt import build_user_prompt, count_todos, sanitize_report, system_prompt
from .triage import Triage, mail_sources
from .triage import analyse as analyse_mail

__all__ = ["Assistant", "DraftProposal", "MailSession"]

log = logging.getLogger(__name__)

MailSession = Callable[[], AbstractAsyncContextManager[MailBackend]]

SAVE_FAILED = (
    "Speichern fehlgeschlagen. Der Entwurf könnte trotzdem angelegt worden sein – "
    "bitte zuerst im Entwürfe-Ordner deines Postfachs nachsehen, bevor du erneut speicherst."
)


Tracer = Callable[[str, dict[str, Any]], None]


class _TracingLLM:
    """Passes calls through and reports exactly what went to the model and what came back."""

    def __init__(self, llm: LLMAdapter, trace: Callable[[str, dict[str, Any]], None]):
        self._llm, self._trace = llm, trace

    async def complete(self, system: str, user: str) -> Completion:
        self._trace("an_modell", {"system": system, "user": user})
        completion = await self._llm.complete(system, user)
        self._trace("vom_modell", {"modell": completion.model, "text": completion.text})
        return completion


class Assistant:
    def __init__(
        self,
        settings: Settings,
        mail_session: MailSession,
        llm: LLMAdapter,
        gate: ApprovalGate | None = None,
        audit: AuditLog | None = None,
        ledger: DraftLedger | None = None,
        tracer: Tracer | None = None,
    ):
        self._settings = settings
        self._mail_session = mail_session
        self._llm = llm
        self._gate = gate or ApprovalGate()
        self._audit = audit or AuditLog()
        self._ledger = ledger or DraftLedger()
        # Optional observer for the data-flow view; receives (step, details). Never sees secrets.
        self.tracer = tracer

    def _trace(self, step: str, details: dict[str, Any]) -> None:
        if self.tracer is not None:
            self.tracer(step, details)

    def _record(self, event: str, **fields: Any) -> None:
        self._audit.record(event, **fields)
        self._trace("protokoll", {"event": event, **{k: v for k, v in fields.items() if v is not None}})

    def _model(self) -> LLMAdapter:
        return _TracingLLM(self._llm, self._trace) if self.tracer is not None else self._llm

    async def _read(self, ref: MessageRef) -> tuple[Email, list[ThreadMessage]]:
        async with self._mail_session() as mail:
            email = await mail.get_email(ref)
            thread = await mail.get_thread(ref)
        if self.tracer is not None:
            self._trace("gelesen", {
                "absender": email.sender, "betreff": email.subject, "zeichen": len(email.body),
                "gekuerzt": email.truncated, "verlauf": len(thread),
            })
            untrusted = [email.sender, email.subject, email.body] + [f"{m.sender}\n{m.subject}\n{m.body}" for m in thread]
            self._trace("bereinigt", {**sanitize_report(untrusted), "hinweise": list(email.notes)})
        return email, thread

    @classmethod
    def from_settings(cls, settings: Settings) -> Assistant:
        data = Path(settings.data_dir)
        if settings.connector == "demo":
            from .demo import DemoMailbox, MockModel

            mailbox = DemoMailbox(data / "demo_entwuerfe.json")
            return cls(
                settings, mailbox.session, MockModel(naive=settings.demo_model == "naiv"),
                audit=AuditLog(data / "audit.jsonl"), ledger=DraftLedger(data / "mailassist.sqlite"),
            )
        llm: LLMAdapter
        if settings.use_mock:
            from .demo import MockModel

            llm = MockModel(naive=settings.demo_model == "naiv")
        else:
            llm = NanoGPTClient(
                api_key=settings.nanogpt_api_key,
                model=settings.model,
                base_url=settings.nanogpt_base_url,
                max_tokens=settings.max_tokens,
            )
        if settings.connector == "imap":
            from .imap import ImapMail

            own = sorted(settings.own_addresses)
            return cls(
                settings,
                lambda: ImapMail(
                    settings.imap_host, settings.imap_user, settings.imap_password, port=settings.imap_port,
                    own_address=settings.imap_user if "@" in settings.imap_user else (own[0] if own else None),
                    inbox=settings.inbox_folder, drafts_folder=settings.imap_drafts_folder,
                    sent_folder=settings.imap_sent_folder,
                ),
                llm,
                audit=AuditLog(data / "audit.jsonl"),
                ledger=DraftLedger(data / "mailassist.sqlite"),
            )
        return cls(
            settings,
            lambda: BaryonMail(
                settings.baryon_command,
                thread_folder=settings.search_folder,
                expected_version=settings.baryon_version or None,
            ),
            llm,
            audit=AuditLog(data / "audit.jsonl"),
            ledger=DraftLedger(data / "mailassist.sqlite"),
        )

    @property
    def mail_session(self) -> MailSession:
        return self._mail_session

    @property
    def llm(self) -> LLMAdapter:
        return self._llm

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
        email, thread = await self._read(ref)
        # Reloaded per draft so edits and expiry dates apply without a restart.
        entries, skipped = load_knowledge(self._settings.knowledge_dir)
        for reason in skipped:
            log.info("knowledge skipped: %s", reason)  # also shown by "mailassist check"
        chosen = select_facts(entries, f"{email.subject}\n{email.body}\n{instruction or ''}")
        self._trace("wissensbasis", {"verwendet": [e.title for e in chosen], "uebersprungen": skipped})
        facts = facts_block(chosen) or None
        user = build_user_prompt(
            email,
            thread,
            instruction,
            own_addresses=self._settings.own_addresses,
            previous_draft=previous.body if previous else None,
            change_request=change_request,
            company_facts=facts,
        )
        completion = await self._model().complete(system_prompt(self._settings.signature), user)
        trusted = [instruction or "", change_request or "", facts or "", self._settings.signature or ""]
        sources = mail_sources(email, thread) + [date_variants(email.date)] + trusted
        proposal = DraftProposal(
            ref=ref,
            subject=email.subject,
            recipient=email.sender,
            body=completion.text,
            model=completion.model,
            message_id=email.message_id,
            warnings=tuple(unsupported_claims(completion.text, sources, trusted_sources=trusted)),
        )
        self._gate.register(proposal, owner)
        self._trace("pruefung", {"warnungen": list(proposal.warnings), "todos": proposal.todo_count})
        self._record(
            "proposed", proposal_id=proposal.id, message_key=message_key(email.message_id, ref),
            content_hash=proposal.content_hash[:16], model=proposal.model, owner=owner,
            todos=proposal.todo_count, warnings=len(proposal.warnings), facts=len(entries),
        )
        log.info(
            "proposal id=%s uid=%s thread=%d facts=%d todos=%d warnings=%d",
            proposal.id, ref.uid, len(thread), len(entries), count_todos(proposal.body), len(proposal.warnings),
        )
        return proposal

    async def analyse(self, ref: MessageRef) -> tuple[Triage, str]:
        """Classify and extract; read-only, nothing is stored or executed."""
        email, thread = await self._read(ref)
        triage, model = await analyse_mail(self._model(), email, thread, self._settings.own_addresses)
        facts = [f for f in triage.facts() if f.status != "unknown"]
        self._trace("belegpruefung", {
            "fakten": len(facts), "belegt": sum(f.verified for f in facts),
            "herabgestuft": [f.value for f in facts if not f.verified],
            "manipulationsverdacht": triage.injection_suspected,
        })
        self._record(
            "analysed", message_key=message_key(email.message_id, ref), model=model, category=triage.category,
            injection=triage.injection_suspected, facts=len(facts), verified=sum(f.verified for f in facts),
        )
        log.info(
            "triage uid=%s category=%s facts=%d verified=%d injection=%s",
            ref.uid, triage.category, len(facts), sum(f.verified for f in facts), triage.injection_suspected,
        )
        return triage, model

    def current_proposal(self, proposal_id: str, owner: str) -> DraftProposal:
        return self._gate.current(proposal_id, owner)

    def discard(self, proposal_id: str, owner: str) -> None:
        try:
            self._gate.discard(proposal_id, owner)
        except ApprovalError as exc:
            self._record("refused", proposal_id=proposal_id, owner=owner, reason=exc.code)
            raise
        self._record("discarded", proposal_id=proposal_id, owner=owner)
        log.info("proposal id=%s discarded", proposal_id)

    async def save(self, proposal: DraftProposal, owner: str = "cli", force: bool = False) -> SavedDraft:
        return await self.save_approved(proposal.id, proposal.content_hash, owner, force=force)

    async def save_approved(self, proposal_id: str, content_hash: str, owner: str, force: bool = False) -> SavedDraft:
        try:
            proposal = self._gate.verify(proposal_id, content_hash, owner)
            key = message_key(proposal.message_id, proposal.ref)
            previous = self._ledger.last_saved(key)
            if previous and not force:
                raise DuplicateDraftError(
                    f"Zu dieser Mail wurde bereits ein Entwurf gespeichert ({previous[:16].replace('T', ' ')} UTC). "
                    "Trotzdem einen weiteren speichern?",
                    saved_at=previous,
                )
            proposal = self._gate.begin(proposal_id, content_hash, owner)
        except ApprovalError as exc:
            self._record("refused", proposal_id=proposal_id, owner=owner, reason=exc.code)
            raise
        try:
            async with self._mail_session() as mail:
                saved = await mail.save_reply_draft(proposal.ref, proposal.body)
        except MailError as exc:
            self._gate.finish(proposal_id, ok=False)
            self._record("save_failed", proposal_id=proposal_id, owner=owner, message_key=key)
            raise MailError(SAVE_FAILED, detail=exc.detail or str(exc)) from None
        except BaseException:
            self._gate.finish(proposal_id, ok=False)
            self._record("save_failed", proposal_id=proposal_id, owner=owner, message_key=key)
            raise
        self._gate.finish(proposal_id, ok=True)
        self._ledger.record(key, proposal_id)
        self._record(
            "saved", proposal_id=proposal_id, owner=owner, message_key=key,
            content_hash=proposal.content_hash[:16], reason="forced_duplicate" if previous else None,
        )
        log.info("proposal id=%s saved", proposal_id)
        return saved
