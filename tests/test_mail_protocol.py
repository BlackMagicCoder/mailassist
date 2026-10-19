"""BaryonMail against a real MCP stdio server (tests/fake_baryon.py) with mock mail."""

import json
import sys
from pathlib import Path

import pytest
from mcp.client.stdio import get_default_environment

from mailassist.mail import ALLOWED_TOOLS, BaryonMail, MailError, MessageRef

FAKE = str(Path(__file__).with_name("fake_baryon.py"))


@pytest.fixture
def calls_log(tmp_path):
    return tmp_path / "calls.jsonl"


@pytest.fixture
def mail(calls_log):
    env = {**get_default_environment(), "FAKE_BARYON_LOG": str(calls_log)}
    return BaryonMail(sys.executable, [FAKE], env=env)


def _calls(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_allowlist_contains_no_sending_or_replacing_tools():
    assert ALLOWED_TOOLS == {"search_emails", "get_email", "get_thread", "save_reply_draft"}


async def test_search_read_thread_and_save_draft(mail, calls_log):
    async with mail as m:
        results = await m.search("All Mail", "Termin", 10)
        assert [r.ref.uid for r in results] == [43, 42, 41]
        assert results[0].sender == "Anna Beispiel <anna@example.org>"

        ref = results[0].ref
        email = await m.get_email(ref)
        assert email.subject == "Re: Terminanfrage"
        assert "Dienstag 14 Uhr" in email.body

        thread = await m.get_thread(ref)
        assert [t.uid for t in thread] == [41, 42, 43]

        saved = await m.save_reply_draft(ref, "Dienstag passt.")
        assert saved.subject == "Re: Terminanfrage"
        assert saved.to == ["Anna Beispiel <anna@example.org>"]

    tools = [c["tool"] for c in _calls(calls_log)]
    assert tools == ["search_emails", "get_email", "get_thread", "save_reply_draft"]
    assert _calls(calls_log)[2]["args"]["include_bodies"] is True


async def test_non_allowlisted_tools_are_refused_before_reaching_server(mail, calls_log):
    async with mail as m:
        for tool in ("send_email", "save_draft"):
            with pytest.raises(PermissionError):
                await m._call(tool, {"to": "x@example.org", "body": "hi", "subject": "s"})
    assert _calls(calls_log) == []


async def test_server_errors_become_mail_errors(mail):
    async with mail as m:
        with pytest.raises(MailError):
            await m.get_email(MessageRef("All Mail", 999, 7))


async def test_missing_tools_are_reported(calls_log):
    # A server without the required tools (python -c exits immediately) must not be used silently.
    broken = BaryonMail(sys.executable, ["-c", "pass"], env=get_default_environment())
    with pytest.raises(Exception):
        async with broken:
            pass
