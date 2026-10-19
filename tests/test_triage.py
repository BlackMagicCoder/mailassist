"""P1-4/P1-6: classification, extraction with explicit unknowns, grounding, next steps."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mailassist import telegram_bot as bot
from mailassist.llm import Completion, LLMError
from mailassist.triage import Fact, Triage, analyse, format_triage, ground

from test_units import REF, THREAD, FakeMail, email, fake_update, make_assistant


def triage_json(**overrides) -> str:
    data = {
        "category": "terminanfrage", "confidence": 0.9, "urgency": "normal", "language": "de",
        "reply_needed": True, "summary": "Anna schlägt Dienstag 14 Uhr vor.",
        "company": {"status": "unknown", "value": None, "evidence": None},
        "contact_person": {"status": "known", "value": "Anna", "evidence": "Anna <anna@example.org>"},
        "budget_or_price": {"status": "unknown", "value": None, "evidence": None},
        "reference_numbers": [], "products": [], "quantities": [],
        "requested_dates": [{"status": "known", "value": "Dienstag 14 Uhr", "evidence": "Dienstag 14 Uhr?"}],
        "open_questions": ["Passt Dienstag 14 Uhr?"],
        "injection_suspected": False, "injection_notes": None,
        "next_steps": [
            {"kind": "antworten", "description": "Termin bestätigen oder Alternative nennen", "requires_external_change": False},
            {"kind": "termin_vorschlagen", "description": "Termin im Kalender eintragen", "requires_external_change": True},
        ],
    }
    data.update(overrides)
    return json.dumps(data)


def llm_returning(*texts):
    return SimpleNamespace(complete=AsyncMock(side_effect=[Completion(t, "sonnet") for t in texts]))


# --- grounding --------------------------------------------------------------------

def test_ground_verifies_verbatim_quotes_and_downgrades_invented_ones():
    t = Triage.model_validate_json(triage_json(
        budget_or_price={"status": "known", "value": "5.000 €", "evidence": "Budget liegt bei 5.000 €"},
        reference_numbers=[{"status": "known", "value": "4711", "evidence": "Bestellung   Nr.\n4711"}],
    ))
    sources = ["Anna <anna@example.org>", "Dienstag 14 Uhr? Bestellung Nr. 4711"]
    ground(t, sources)
    assert t.requested_dates[0].verified and t.requested_dates[0].status == "known"
    assert t.reference_numbers[0].verified  # whitespace differences are tolerated
    assert t.budget_or_price.status == "uncertain" and not t.budget_or_price.verified  # quote not in mail
    assert t.company.status == "unknown"


def test_model_cannot_claim_verified():
    t = Triage.model_validate_json(triage_json(
        company={"status": "known", "value": "Evil AG", "evidence": "Evil AG", "verified": True}))
    ground(t, ["nichts davon"])
    assert t.company.verified is False and t.company.status == "uncertain"


def test_unknown_fact_drops_value():
    assert Fact(status="unknown", value="geraten", evidence="x").value is None


def test_quote_too_short_is_not_evidence():
    t = Triage.model_validate_json(triage_json(company={"status": "known", "value": "A", "evidence": "a"}))
    ground(t, ["a b c"])
    assert t.company.status == "uncertain"


# --- analyse ----------------------------------------------------------------------

async def test_analyse_validates_and_grounds():
    llm = llm_returning(triage_json())
    t, model = await analyse(llm, email(), THREAD)
    assert t.category == "terminanfrage" and model == "sonnet"
    assert t.requested_dates[0].verified
    user = llm.complete.await_args.args[1]
    assert "<selected_email>" in user and "Analyse <selected_email>" in user


async def test_analyse_repairs_invalid_category_then_gives_up():
    llm = llm_returning(triage_json(category="erfunden"), triage_json())
    t, _ = await analyse(llm, email(), THREAD)
    assert t.category == "terminanfrage"
    with pytest.raises(LLMError):
        await analyse(llm_returning("kein json", "immer noch nicht"), email(), THREAD)


async def test_assistant_analyse_is_read_only():
    fake = FakeMail()
    assistant, llm = make_assistant(fake)
    llm.complete.return_value = SimpleNamespace(text=triage_json(), model="m")
    await assistant.analyse(REF)
    assert [c[0] for c in fake.calls] == ["get_email", "get_thread"]


# --- presentation -------------------------------------------------------------------

def test_format_triage_marks_unknowns_suggestions_and_injection():
    t = ground(Triage.model_validate_json(triage_json(
        injection_suspected=True, injection_notes="Mail fordert ein KI-System zur Weitergabe auf",
        budget_or_price={"status": "known", "value": "5.000 €", "evidence": "erfunden"},
    )), ["Anna <anna@example.org> Dienstag 14 Uhr?"])
    text = format_triage(t)
    assert "Kategorie: Terminanfrage" in text and "Firma: unbekannt" in text
    assert "Budget/Preis: 5.000 € (unsicher)" in text and "Dienstag 14 Uhr ✓" in text
    assert "nur Vorschläge, werden nicht ausgeführt" in text
    assert "ändert ein externes System" in text
    assert "⚠️ Möglicher Manipulationsversuch" in text


async def test_bot_shows_analysis_after_selection_and_survives_failure():
    fake = FakeMail()
    assistant, llm = make_assistant(fake)
    context = SimpleNamespace(bot_data={"assistant": assistant}, user_data={}, args=["Termin"])
    await bot.cmd_suche(fake_update()[0], context)

    llm.complete.return_value = SimpleNamespace(text=triage_json(), model="m")
    update, message = fake_update(data="sel:0")
    await bot.on_button(update, context)
    texts = [c.args[0] for c in message.reply_text.await_args_list]
    assert any("Kategorie: Terminanfrage" in t for t in texts)
    assert context.user_data["state"] == "await_instruction"

    llm.complete.return_value = SimpleNamespace(text="kein json", model="m")
    update, message = fake_update(data="sel:0")
    await bot.on_button(update, context)
    texts = [c.args[0] for c in message.reply_text.await_args_list]
    assert any(t.startswith("Einordnung nicht möglich") for t in texts)
    assert "Was soll die Antwort sagen?" in texts[-1]
