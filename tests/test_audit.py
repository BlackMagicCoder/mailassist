"""P1-7: audit trail without content, duplicate-draft detection, refusals logged."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mailassist import telegram_bot as bot
from mailassist.approval import ApprovalError, DuplicateDraftError
from mailassist.audit import AuditLog, DraftLedger
from mailassist.core import Assistant

from test_triage import triage_json
from test_units import REF, FakeMail, button_data, email, fake_update, settings

OWNER = "telegram:111"


def assistant_with(tmp_path, fake, text="Hallo Anna, Dienstag passt."):
    @asynccontextmanager
    async def session():
        yield fake

    llm = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(text=text, model="sonnet")))
    a = Assistant(settings(), session, llm, audit=AuditLog(tmp_path / "audit.jsonl"),
                  ledger=DraftLedger(tmp_path / "ledger.sqlite"))
    return a, llm


def events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


async def test_audit_trail_records_metadata_but_no_content(tmp_path):
    fake = FakeMail()
    assistant, llm = assistant_with(tmp_path, fake)
    llm.complete.return_value = SimpleNamespace(text=triage_json(), model="sonnet")
    await assistant.analyse(REF)
    llm.complete.return_value = SimpleNamespace(text="Hallo Anna, Dienstag passt.", model="sonnet")
    proposal = await assistant.propose(REF, "GEHEIME ANWEISUNG", owner=OWNER)
    with pytest.raises(ApprovalError):
        await assistant.save_approved(proposal.id, "0" * 16, OWNER)
    await assistant.save(proposal, owner=OWNER)

    log = events(tmp_path)
    assert [e["event"] for e in log] == ["analysed", "proposed", "refused", "saved"]
    assert log[2]["reason"] == "hash_mismatch"
    assert log[1]["proposal_id"] == proposal.id and log[1]["content_hash"] == proposal.content_hash[:16]
    assert log[0]["category"] == "terminanfrage" and log[0]["message_key"] == log[1]["message_key"]
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    for content in ("Hallo Anna", "Dienstag", "GEHEIME ANWEISUNG", "anna@example.org", "Terminanfrage", email().body):
        assert content not in raw


def test_audit_rejects_unexpected_fields(tmp_path):
    with pytest.raises(ValueError):
        AuditLog(tmp_path / "a.jsonl").record("proposed", body="Mailtext")


async def test_second_draft_for_same_message_needs_confirmation(tmp_path):
    fake = FakeMail()
    assistant, _ = assistant_with(tmp_path, fake)
    first = await assistant.propose(REF, "x", owner=OWNER)
    await assistant.save(first, owner=OWNER)

    second = await assistant.propose(REF, "y", owner=OWNER)
    with pytest.raises(DuplicateDraftError):
        await assistant.save(second, owner=OWNER)
    assert [c[0] for c in fake.calls].count("save_reply_draft") == 1
    assistant.current_proposal(second.id, OWNER)  # still open: the warning changed no state

    await assistant.save(second, owner=OWNER, force=True)
    assert [c[0] for c in fake.calls].count("save_reply_draft") == 2
    log = events(tmp_path)
    assert [e["event"] for e in log].count("refused") == 1 and log[-1]["reason"] == "forced_duplicate"


async def test_ledger_survives_restart(tmp_path):
    fake = FakeMail()
    assistant, _ = assistant_with(tmp_path, fake)
    await assistant.save(await assistant.propose(REF, "x"))
    restarted, _ = assistant_with(tmp_path, fake)
    with pytest.raises(DuplicateDraftError):
        await restarted.save(await restarted.propose(REF, "x"))


async def test_failed_save_is_audited_and_not_recorded_as_saved(tmp_path):
    fake = FakeMail()
    assistant, _ = assistant_with(tmp_path, fake)
    proposal = await assistant.propose(REF, "x", owner=OWNER)
    fake.save_reply_draft = AsyncMock(side_effect=RuntimeError("bridge down"))
    with pytest.raises(RuntimeError):
        await assistant.save(proposal, owner=OWNER)
    assert events(tmp_path)[-1]["event"] == "save_failed"
    fake.save_reply_draft = AsyncMock(return_value=SimpleNamespace(subject="Re", to=["a"]))
    await assistant.save(proposal, owner=OWNER)  # no duplicate warning: nothing was recorded as saved


async def test_discard_is_audited(tmp_path):
    assistant, _ = assistant_with(tmp_path, FakeMail())
    proposal = await assistant.propose(REF, "x", owner=OWNER)
    assistant.discard(proposal.id, OWNER)
    with pytest.raises(ApprovalError):
        assistant.discard(proposal.id, OWNER)
    assert [e["event"] for e in events(tmp_path)][-2:] == ["discarded", "refused"]


async def test_telegram_duplicate_asks_and_force_button_saves(tmp_path):
    fake = FakeMail()
    assistant, _ = assistant_with(tmp_path, fake)
    await assistant.save(await assistant.propose(REF, "x", owner=OWNER), owner=OWNER)

    context = SimpleNamespace(bot_data={"assistant": assistant}, user_data={}, args=["Termin"])
    await bot.cmd_suche(fake_update()[0], context)
    context.user_data["selected"] = context.user_data["results"][0]
    context.user_data["state"] = "await_instruction"
    update, message = fake_update(text="nochmal")
    await bot.on_text(update, context)

    update, message = fake_update(data=button_data(message)["Speichern"])
    await bot.on_button(update, context)
    assert "bereits ein Entwurf" in message.reply_text.await_args.args[0]
    assert [c[0] for c in fake.calls].count("save_reply_draft") == 1

    update, message = fake_update(data=button_data(message)["Trotzdem speichern"])
    await bot.on_button(update, context)
    assert [c[0] for c in fake.calls].count("save_reply_draft") == 2
