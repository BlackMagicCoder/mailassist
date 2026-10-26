"""P2-6: `mailassist check` reports status without mail content."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mailassist.health import format_checks, run_checks
from mailassist.llm import Completion, LLMError
from mailassist.mail import MailError

from test_knowledge_checks import DEMO
from test_units import FakeMail, settings


def session_for(mail):
    @asynccontextmanager
    async def session():
        yield mail

    return session


async def test_all_green_shows_counts_but_no_content():
    llm = SimpleNamespace(complete=AsyncMock(return_value=Completion("OK", "anthropic/claude-sonnet-x")))

    async def probe():
        return "@mailassist_test_bot"

    results = await run_checks(settings(knowledge_dir=str(DEMO)), session_for(FakeMail()), llm, probe)
    text = format_checks(results)
    assert all(r.ok for r in results) and "✗" not in text
    assert "freigegebene Einträge" in text and "übersprungen" in text and "@mailassist_test_bot" in text
    assert "Terminanfrage" not in text and "anna@example.org" not in text  # no mail content


async def test_failures_are_red_and_explained():
    broken = SimpleNamespace(search=AsyncMock(side_effect=MailError("Postfach-Zugriff fehlgeschlagen (search_emails).")))
    llm = SimpleNamespace(complete=AsyncMock(side_effect=LLMError("Bedrock antwortete mit HTTP 401 (API-Schlüssel prüfen).",
                                                                  detail="use case details have not been submitted")))
    results = await run_checks(settings(), session_for(broken), llm)
    status = {r.name: r.ok for r in results}
    assert status["Postfach (Baryon/Bridge)"] is False and status["Bedrock"] is False
    assert "HTTP 401" in format_checks(results) and "use case details" in format_checks(results)


async def test_missing_baryon_binary_is_reported():
    @asynccontextmanager
    async def session():
        raise FileNotFoundError("baryon-mcp")
        yield  # pragma: no cover

    results = await run_checks(settings(), session)
    assert any(r.name.startswith("Postfach") and not r.ok and "nicht gefunden" in r.info for r in results)
