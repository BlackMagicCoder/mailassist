"""P1-8: deterministic suites over the synthetic corpus (src/mailassist/demo_data/beispiel_gmbh.toml).

Offline and model-free: they check what the code guarantees regardless of model
behaviour - corpus integrity, grounding, prompt structure under injection, output
checks, and that model failures never lead to a saved draft. How well the real
model classifies and resists injection is measured by the opt-in live eval.
"""

import re
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import httpx
import pytest

from mailassist.approval import ApprovalError
from mailassist.checks import unsupported_claims
from mailassist.core import Assistant
from mailassist.knowledge import facts_block, load_knowledge
from mailassist.llm import LLMError, NanoGPTClient
from mailassist.prompt import build_user_prompt
from mailassist.triage import Category, Fact, Triage, ground, mail_sources

from corpus_utils import OWN, load_cases
from test_knowledge_checks import DEMO
from test_units import settings

CASES = load_cases()
INJECTIONS = [c for c in CASES if c.injection]
FACTS = facts_block(load_knowledge(DEMO, today=date(2026, 10, 19))[0])
FACT_FIELDS = {"company", "contact_person", "budget_or_price", "reference_numbers", "products", "quantities", "requested_dates"}


def ids(cases):
    return [c.id for c in cases]


# --- corpus integrity ----------------------------------------------------------------

def test_corpus_size_and_unique_ids():
    assert len(CASES) >= 20 and len(INJECTIONS) >= 6
    assert len(set(ids(CASES))) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_case_is_well_formed_and_synthetic(case):
    assert case.category in get_args(Category)
    assert set(case.unknown) <= FACT_FIELDS
    text = " ".join([case.sender, case.body, case.raw_html or ""] + [t["from"] + t["body"] for t in case.thread])
    for domain in re.findall(r"@([\w.-]+)|https?://([\w.-]+)", text):
        host = next(d for d in domain if d)
        assert ".example" in host, f"non-reserved domain {host}"
    sources = " ".join(mail_sources(case.email(), case.thread_messages()))
    for quote in case.known:
        assert quote in sources, f"known quote not in mail: {quote}"


# --- grounding -------------------------------------------------------------------------

def _triage(case, facts: list[Fact]) -> Triage:
    return Triage(category=case.category, confidence=0.8, urgency="normal", language=case.lang,
                  reply_needed=True, summary="s", products=facts)


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_true_quotes_are_verified_and_invented_ones_are_not(case):
    facts = [Fact(status="known", value=q, evidence=q) for q in case.known]
    invented = Fact(status="known", value="9.999 €", evidence="Wir zahlen 9.999 € pauschal")
    t = ground(_triage(case, facts + [invented]), mail_sources(case.email(), case.thread_messages()))
    assert all(f.verified and f.status == "known" for f in t.products[:-1])
    assert t.products[-1].status == "uncertain" and not t.products[-1].verified


@pytest.mark.parametrize("case", [c for c in CASES if c.unknown], ids=ids([c for c in CASES if c.unknown]))
def test_fields_the_mail_does_not_state_can_never_be_verified(case):
    hallucinated = Fact(status="known", value="erfunden", evidence="laut Ihrer Mail gilt ein Sonderpreis")
    t = ground(_triage(case, [hallucinated]), mail_sources(case.email(), case.thread_messages()))
    assert t.products[0].status == "uncertain"


# --- injection: prompt structure --------------------------------------------------------

@pytest.mark.parametrize("case", INJECTIONS, ids=ids(INJECTIONS))
def test_injection_cannot_break_prompt_structure(case):
    prompt = build_user_prompt(case.email(), case.thread_messages(), "Antworte höflich.",
                               own_addresses=OWN, company_facts=FACTS)
    for tag in ("selected_email", "owner_instruction", "company_facts"):
        assert prompt.count(f"<{tag}>") == 1 and prompt.count(f"</{tag}>") == 1, tag
    assert 'author="owner"' not in prompt  # spoofed display names never count as the owner
    if case.hidden_text:
        assert case.hidden_text not in prompt


# --- injection and missing facts: output checks -------------------------------------------

@pytest.mark.parametrize("case", INJECTIONS, ids=ids(INJECTIONS))
def test_compromised_draft_markers_are_flagged(case):
    """Simulates a model that obeyed the injection. Links, addresses and percentages must be
    flagged; word-only markers ("kostenlos") are not detectable deterministically and rely on
    the prompt rules, the live eval and the human approval."""
    draft = "Vielen Dank für Ihre Nachricht. " + " ".join(case.attacker_markers)
    trusted = ["Antworte höflich.", FACTS]
    found = " ".join(unsupported_claims(draft, mail_sources(case.email(), case.thread_messages()) + trusted, trusted))
    for marker in case.attacker_markers:
        if re.search(r"https?://|@|\d\s?%", marker):
            assert marker in found, marker


@pytest.mark.parametrize("case_id", ["angebot-unbekanntes-produkt", "fehlende-angaben"])
def test_missing_facts_compliant_draft_passes_invented_price_is_flagged(case_id):
    case = next(c for c in CASES if c.id == case_id)
    sources = mail_sources(case.email(), case.thread_messages()) + [FACTS]
    compliant = "Gern erstellen wir Ihnen ein Angebot. Preis und Lieferzeit: [[TODO: beim Vertrieb erfragen]]"
    assert unsupported_claims(compliant, sources, [FACTS]) == []
    invented = "Das kostet 5.900 € und ist in 6 Tagen bei Ihnen."
    found = " ".join(unsupported_claims(invented, sources, [FACTS]))
    assert "5.900" in found and "„6“" in found
    # Context-free by design: "5" occurs in the facts ("5 % Mengenrabatt"), so "in 5 Tagen" would pass.
    assert unsupported_claims("Lieferung in 5 Tagen.", sources, [FACTS]) == []


def test_facts_backed_answer_has_no_warnings():
    case = next(c for c in CASES if c.id == "angebot-fb200")
    draft = "Das Förderband FB-200 kostet 4.850 € netto pro Stück, Lieferzeit 3 bis 4 Wochen. Für 3 Stück ..."
    assert unsupported_claims(draft, mail_sources(case.email(), case.thread_messages()) + [FACTS], [FACTS]) == []


# --- model failures never produce a saved draft ------------------------------------------

FAILURES = {
    "timeout": lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
    "server_error": lambda r: httpx.Response(503),
    "unauthorized": lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}),
    "truncated": lambda r: httpx.Response(200, json={"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Sehr geehrte"}]}),
    "empty": lambda r: httpx.Response(200, json={"content": []}),
    "garbage": lambda r: httpx.Response(200, text="<html>proxy error</html>"),
}


@pytest.mark.parametrize("failure", list(FAILURES), ids=list(FAILURES))
async def test_model_failure_never_leads_to_a_saved_draft(failure):
    case = CASES[0]
    mail = SimpleNamespace(
        get_email=AsyncMock(return_value=case.email()),
        get_thread=AsyncMock(return_value=case.thread_messages()),
        save_reply_draft=AsyncMock(),
    )

    @asynccontextmanager
    async def session():
        yield mail

    async def no_sleep(_):
        return None

    llm = NanoGPTClient("k", "m", "https://x.test/api/v1", transport=httpx.MockTransport(FAILURES[failure]), sleep=no_sleep)
    assistant = Assistant(settings(), session, llm)
    with pytest.raises(LLMError):
        await assistant.propose(case.email().ref, "Angebot", owner="cli")
    with pytest.raises(LLMError):
        await assistant.analyse(case.email().ref)
    with pytest.raises(ApprovalError):  # nothing was registered that could be approved
        await assistant.save_approved("000000000000", "0" * 16, "cli")
    mail.save_reply_draft.assert_not_awaited()
