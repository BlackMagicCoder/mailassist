"""Approval gate: an action with external effect (today only "save draft") runs only
for a registered proposal, by its owner, with a matching content hash, once.

A newer proposal from the same owner supersedes older ones, so a button under an
outdated draft can never save anything.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .errors import AssistantError
from .models import DraftProposal

MIN_HASH_PREFIX = 16


class ApprovalError(AssistantError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class CommitmentConfirmationError(ApprovalError):
    """The draft contains unbacked business commitments; the owner must confirm them explicitly first."""

    def __init__(self, message: str):
        super().__init__(message, code="unconfirmed_commitment")


class DuplicateDraftError(ApprovalError):
    """A draft for this message was saved before; saving again needs explicit confirmation."""

    def __init__(self, message: str, saved_at: str):
        super().__init__(message, code="duplicate")
        self.saved_at = saved_at


@dataclass
class _Ticket:
    proposal: DraftProposal
    owner: str
    state: str = "pending"  # pending | saving | saved | failed | superseded | discarded
    commitments_confirmed: bool = False


_REFUSALS = {
    "saving": "Dieser Entwurf wird gerade gespeichert.",
    "saved": "Dieser Entwurf wurde bereits gespeichert.",
    "superseded": "Dieser Entwurf ist veraltet – es gibt einen neueren. Bitte den aktuellen verwenden.",
    "discarded": "Dieser Entwurf wurde verworfen.",
}


class ApprovalGate:
    def __init__(self, max_tickets: int = 200):
        self._tickets: OrderedDict[str, _Ticket] = OrderedDict()
        self._max = max_tickets

    def register(self, proposal: DraftProposal, owner: str) -> None:
        for ticket in self._tickets.values():
            if ticket.owner == owner and ticket.state in ("pending", "failed"):
                ticket.state = "superseded"
        self._tickets[proposal.id] = _Ticket(proposal, owner)
        while len(self._tickets) > self._max:
            self._tickets.popitem(last=False)

    def _open_ticket(self, proposal_id: str, owner: str) -> _Ticket:
        ticket = self._tickets.get(proposal_id)
        if ticket is None or ticket.owner != owner:
            raise ApprovalError("Unbekannter oder abgelaufener Entwurf. Bitte neu erstellen.", code="unknown")
        if ticket.state not in ("pending", "failed"):
            raise ApprovalError(_REFUSALS[ticket.state], code=ticket.state)
        return ticket

    def current(self, proposal_id: str, owner: str) -> DraftProposal:
        """The proposal, if it is still the owner's open one (for revisions)."""
        return self._open_ticket(proposal_id, owner).proposal

    def _matching_ticket(self, proposal_id: str, content_hash: str, owner: str) -> _Ticket:
        ticket = self._open_ticket(proposal_id, owner)
        if len(content_hash) < MIN_HASH_PREFIX or not ticket.proposal.content_hash.startswith(content_hash):
            raise ApprovalError("Freigabe passt nicht zum Entwurfsinhalt. Nichts gespeichert.", code="hash_mismatch")
        return ticket

    def verify(self, proposal_id: str, content_hash: str, owner: str) -> DraftProposal:
        """All approval checks, without changing state."""
        ticket = self._matching_ticket(proposal_id, content_hash, owner)
        if ticket.proposal.critical and not ticket.commitments_confirmed:
            raise CommitmentConfirmationError(
                f"Der Entwurf enthält {len(ticket.proposal.critical)} nicht freigegebene Zusage(n). "
                "Speichern erst nach ausdrücklicher Bestätigung."
            )
        return ticket.proposal

    def confirm_commitments(self, proposal_id: str, content_hash: str, owner: str) -> DraftProposal:
        """The owner accepts the flagged commitments of exactly this text (same id and hash rules as saving)."""
        ticket = self._matching_ticket(proposal_id, content_hash, owner)
        ticket.commitments_confirmed = True
        return ticket.proposal

    def begin(self, proposal_id: str, content_hash: str, owner: str) -> DraftProposal:
        proposal = self.verify(proposal_id, content_hash, owner)
        self._tickets[proposal_id].state = "saving"
        return proposal

    def finish(self, proposal_id: str, ok: bool) -> None:
        ticket = self._tickets.get(proposal_id)
        if ticket is not None and ticket.state == "saving":
            ticket.state = "saved" if ok else "failed"

    def discard(self, proposal_id: str, owner: str) -> DraftProposal:
        ticket = self._open_ticket(proposal_id, owner)
        ticket.state = "discarded"
        return ticket.proposal
