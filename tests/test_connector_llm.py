"""P1-1..P1-3: validated connector responses, complete threads, LLM retries and JSON mode."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.client.stdio import get_default_environment
from pydantic import BaseModel

from mailassist.core import Assistant
from mailassist.llm import Completion, LLMError, NanoGPTClient, complete_json, parse_json
from mailassist.mail import BaryonMail, MailError, _parse, _SearchPage

from test_units import settings

FAKE = str(Path(__file__).with_name("fake_baryon.py"))


def fake_baryon(tmp_path, **kwargs) -> BaryonMail:
    env = {**get_default_environment(), "FAKE_BARYON_LOG": str(tmp_path / "calls.jsonl")}
    return BaryonMail(sys.executable, [FAKE], env=env, **kwargs)


# --- P1-2: complete thread ----------------------------------------------------------

async def test_thread_from_inbox_includes_own_reply_from_sent(tmp_path):
    async with fake_baryon(tmp_path) as legacy:
        inbox = await legacy.search("INBOX", None, 10)
        assert [r.ref.uid for r in inbox] == [3, 1]
        without = await legacy.get_thread(inbox[0].ref)
    assert [m.sender for m in without] == ["Anna Beispiel <anna@example.org>"] * 2  # the old bug: Sent missing

    async with fake_baryon(tmp_path, thread_folder="All Mail") as mail:
        thread = await mail.get_thread(inbox[0].ref)
    assert [m.message_id for m in thread] == ["m41@example.org", "m42@example.org", "m43@example.org"]


async def test_draft_prompt_contains_own_reply_and_selected_mail_once(tmp_path):
    llm = SimpleNamespace(complete=AsyncMock(return_value=Completion("Dienstag passt. [[TODO: Name]]", "m")))

    @asynccontextmanager
    async def session():
        async with fake_baryon(tmp_path, thread_folder="All Mail") as mail:
            yield mail

    assistant = Assistant(settings(), session, llm)
    async with fake_baryon(tmp_path) as mail:
        ref = (await mail.search("INBOX", None, 10))[0].ref
    proposal = await assistant.propose(ref, "zusagen")
    user = llm.complete.await_args.args[1]
    assert '<message author="owner">\nFrom: Ich <me@example.net>' in user
    assert "Gern, welcher Tag wäre gut?" in user
    assert user.count("Dienstag 14 Uhr?") == 1
    assert proposal.message_id == "m43@example.org"


# --- P1-1: validated Baryon responses ----------------------------------------------

def test_malformed_baryon_response_becomes_mail_error_without_values():
    bad = {"folder": "INBOX", "uidvalidity": "not-a-number", "emails": [{"uid": "GEHEIMER BETREFF"}]}
    with pytest.raises(MailError) as exc:
        _parse(_SearchPage, bad, "search_emails")
    assert "search_emails" in str(exc.value)
    assert "uidvalidity" in exc.value.detail and "GEHEIM" not in exc.value.detail


# --- P1-3: retries -------------------------------------------------------------------

def scripted(*responses):
    calls = []

    def handler(request):
        calls.append(request)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), calls


OK = httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})


def client(transport, sleeps):
    async def sleep(delay):
        sleeps.append(delay)

    return NanoGPTClient("k", "m", "https://x.test/api/v1", transport=transport, sleep=sleep)


async def test_retries_transient_errors_with_backoff():
    transport, calls = scripted(httpx.Response(503), httpx.ConnectError("down"), OK)
    sleeps = []
    assert (await client(transport, sleeps).complete("s", "u")).text == "ok"
    assert len(calls) == 3 and sleeps == [2.0, 4.0]


async def test_respects_retry_after_and_caps_it():
    transport, _ = scripted(httpx.Response(429, headers={"retry-after": "5"}), httpx.Response(429, headers={"retry-after": "999"}), OK)
    sleeps = []
    await client(transport, sleeps).complete("s", "u")
    assert sleeps == [5.0, 30.0]


async def test_gives_up_after_max_retries():
    transport, calls = scripted(httpx.Response(500))
    with pytest.raises(LLMError, match="HTTP 500"):
        await client(transport, []).complete("s", "u")
    assert len(calls) == 3

    transport, calls = scripted(httpx.ReadTimeout("slow"))
    with pytest.raises(LLMError, match="nicht erreichbar"):
        await client(transport, []).complete("s", "u")
    assert len(calls) == 3


async def test_client_errors_are_not_retried():
    transport, calls = scripted(httpx.Response(400, json={"error": "bad"}), OK)
    with pytest.raises(LLMError, match="HTTP 400"):
        await client(transport, []).complete("s", "u")
    assert len(calls) == 1


# --- P1-3: JSON mode -----------------------------------------------------------------

class Answer(BaseModel):
    category: str
    count: int


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"category": "a", "count": 2}\n```', Answer).count == 2
    assert parse_json('Hier: {"category": "a", "count": 2} fertig', Answer).category == "a"
    with pytest.raises(ValueError):
        parse_json("kein json", Answer)


async def test_complete_json_repairs_once():
    llm = SimpleNamespace(complete=AsyncMock(side_effect=[
        Completion('{"category": "a", "count": "GEHEIM"}', "m1"),
        Completion('{"category": "a", "count": 3}', "m2"),
    ]))
    answer, model = await complete_json(llm, "sys", "user", Answer)
    assert answer.count == 3 and model == "m2"
    repair_prompt = llm.complete.await_args_list[1].args[1]
    assert "<format_error>" in repair_prompt and "count: int_parsing" in repair_prompt
    assert repair_prompt.count("GEHEIM") == 0


async def test_complete_json_gives_up_after_repair():
    llm = SimpleNamespace(complete=AsyncMock(return_value=Completion("Entschuldigung, das kann ich nicht.", "m")))
    with pytest.raises(LLMError, match="strukturierte"):
        await complete_json(llm, "sys", "user", Answer)
    assert llm.complete.await_count == 2
