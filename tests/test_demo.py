"""Demo mode: synthetic mailbox, mock model, data-flow view, CLI end to end."""

import json
import re
from collections import Counter
from importlib import resources

import pytest

from mailassist import cli
from mailassist.config import Settings
from mailassist.core import Assistant
from mailassist.dataflow import DataFlow
from mailassist.demo import DemoMailbox, MockModel, load_demo_messages
from mailassist.health import format_checks, run_checks
from mailassist.triage import analyse

from corpus_utils import load_cases


def demo_data(name: str) -> str:
    return resources.files("mailassist").joinpath("demo_data", name).read_text(encoding="utf-8")


# --- data -----------------------------------------------------------------------------

def test_demo_data_is_synthetic_licensed_and_uses_reserved_domains():
    gretel = json.loads(demo_data("gretel_de.json"))
    assert len(gretel) >= 20 and all(m["source"] == "gretel" for m in gretel)
    notice = demo_data("NOTICE.md")
    assert "Apache License 2.0" in notice and "Änderungen gegenüber dem Original" in notice
    text = demo_data("gretel_de.json") + demo_data("beispiel_gmbh.toml")
    hosts = {h for pair in re.findall(r"@([\w-]+(?:\.[\w-]+)+)|://([\w-]+(?:\.[\w-]+)+)", text) for h in pair if h}
    assert hosts and all(h.endswith(".example") or ".example." in h or h.startswith("example.") for h in hosts), hosts


def test_folder_uids_are_unique_and_sent_holds_own_replies():
    messages = load_demo_messages()
    for folder in ("INBOX", "Sent", "All Mail"):
        uids = [m.uids[folder] for m in messages if folder in m.uids]
        assert len(uids) == len(set(uids))
    assert len(messages) >= 45 and any(m.own for m in messages)


# --- mailbox ---------------------------------------------------------------------------

async def test_mailbox_search_read_thread_and_local_drafts(tmp_path):
    box = DemoMailbox(tmp_path / "drafts.json")
    newest = await box.search("INBOX", None, 3)
    assert len(newest) == 3 and newest[0].date >= newest[1].date >= newest[2].date

    hidden = (await box.search("INBOX", "Wartungsvertrag Basis", 5))[0]
    email = await box.get_email(hidden.ref)
    assert "gratis" not in email.body and any("versteckte" in n for n in email.notes)

    log_mail = (await box.search("INBOX", "Ihre Anfrage an den Kundenservice", 1))[0]
    thread = await box.get_thread(log_mail.ref)
    assert len(thread) >= 2 and any("service@gretel-demo.example" in m.sender for m in thread)

    saved = await box.save_reply_draft(hidden.ref, "Entwurf")
    assert saved.subject.startswith("Re:") and json.loads((tmp_path / "drafts.json").read_text(encoding="utf-8"))[0]["body"] == "Entwurf"


# --- mock model ------------------------------------------------------------------------

async def test_mock_triage_is_valid_for_every_demo_mail():
    box, model = DemoMailbox(), MockModel()
    inbox = await box.search("INBOX", None, 100)
    assert len(inbox) >= 40
    for summary in inbox:
        email = await box.get_email(summary.ref)
        triage, name = await analyse(model, email, await box.get_thread(summary.ref))
        assert name == "mock/regelbasiert" and all(f.verified for f in triage.facts() if f.status == "known")


async def test_mock_flags_most_injection_mails_and_classifies_reasonably():
    box, model = DemoMailbox(), MockModel()
    cases = load_cases()
    hits, flagged = Counter(), 0
    for case in cases:
        summary = (await box.search("INBOX", case.subject, 50))
        summary = next(s for s in summary if s.subject == case.subject and s.sender == case.sender)
        triage, _ = await analyse(model, await box.get_email(summary.ref), await box.get_thread(summary.ref))
        hits[triage.category == case.category] += 1
        flagged += case.injection and triage.injection_suspected
    print(f"mock category accuracy {hits[True]}/{len(cases)}, injections flagged {flagged}/{sum(c.injection for c in cases)}")
    assert hits[True] / len(cases) >= 0.6  # rule-based stand-in, not a quality claim about the real model
    assert flagged >= 4


async def test_naive_mock_obeys_injection_and_checks_catch_it(tmp_path):
    settings = Settings.for_demo({"MAILASSIST_DEMO_DATA_DIR": str(tmp_path)}, naive=True)
    assistant = Assistant.from_settings(settings)
    summary = next(s for s in await assistant.search("Ignoriere alle bisherigen", 5) if s.subject == "Angebot FB-200")
    proposal = await assistant.propose(summary.ref, "Angebot zusagen")
    assert "https://pay.evil.example/rechnung" in proposal.body and "50 %" in proposal.body
    warnings = " ".join(proposal.warnings)
    assert "https://pay.evil.example/rechnung" in warnings and "50 %" in warnings

    careful = Assistant.from_settings(Settings.for_demo({"MAILASSIST_DEMO_DATA_DIR": str(tmp_path / "c")}))
    proposal = await careful.propose(summary.ref, "Angebot zusagen")
    assert "evil" not in proposal.body and "50 %" not in proposal.body


def test_demo_settings_never_touch_real_configuration(tmp_path):
    real = {"NANOGPT_API_KEY": "", "MAILASSIST_DATA_DIR": "data", "MAILASSIST_KNOWLEDGE_DIR": "knowledge"}
    settings = Settings.for_demo(real)
    assert settings.connector == "demo" and settings.model == "mock"
    assert settings.data_dir.replace("\\", "/") == "data/demo"
    assert settings.knowledge_dir.replace("\\", "/") == "examples/knowledge"


# --- data flow -------------------------------------------------------------------------

async def test_data_flow_records_every_step_and_counts_defused_tags(tmp_path):
    assistant = Assistant.from_settings(Settings.for_demo({"MAILASSIST_DEMO_DATA_DIR": str(tmp_path)}))
    flow = DataFlow()
    assistant.tracer = flow
    summary = next(s for s in await assistant.search("Edelstahl", 5) if s.subject == "Rückfrage")
    await assistant.analyse(summary.ref)
    proposal = await assistant.propose(summary.ref, "freundlich antworten")
    await assistant.save(proposal)

    steps = [s for s, _ in flow.steps]
    assert steps[:4] == ["gelesen", "bereinigt", "an_modell", "vom_modell"]
    assert {"wissensbasis", "pruefung", "belegpruefung"} <= set(steps) and steps.count("protokoll") == 3
    assert dict(flow.steps)["bereinigt"]["tag_imitationen"] >= 3

    report = flow.write_html(tmp_path, "Test")
    page = report.read_text(encoding="utf-8")
    assert "<mark" in page and "&lt;selected_email&gt;" in page
    assert "<owner_instruction>Gewähre" not in page  # mail content is escaped, never live markup
    summary_text = flow.summary()
    # analyse -> one audit section; propose + save -> their two entries merged into one section
    assert "Nie ans Modell gesendet" in summary_text and summary_text.count("Prüfprotokoll (lokal") == 2


async def test_health_check_labels_demo_honestly(tmp_path):
    settings = Settings.for_demo({"MAILASSIST_DEMO_DATA_DIR": str(tmp_path)})
    assistant = Assistant.from_settings(settings)
    text = format_checks(await run_checks(settings, assistant.mail_session, assistant.llm))
    assert "Postfach (Demo, synthetisch)" in text and "Modell (Mock, lokal)" in text
    assert "Baryon" not in text and "NanoGPT" not in text


# --- CLI end to end ---------------------------------------------------------------------

def test_cli_demo_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "j")
    cli.main(["--demo", "neu", "-n", "5"])
    cli.main(["--demo", "--datenfluss", "antworte", "1"])
    out = capsys.readouterr().out
    assert "DEMO:" in out and "Entwurf gespeichert" in out and "═══ Datenfluss ═══" in out
    assert list((tmp_path / "data" / "demo" / "datenfluss").glob("*.html"))
    assert (tmp_path / "data" / "demo" / "demo_entwuerfe.json").exists()
    with pytest.raises(SystemExit, match="nur zusammen mit --demo"):
        cli.main(["--naiv", "neu"])


# --- --mock: real mailbox, local mock model ----------------------------------------------

def test_mock_settings_need_no_key_and_keep_real_mailbox():
    from mailassist.telegram_bot import help_text

    env = {"MAILASSIST_CONNECTOR": "imap", "IMAP_USER": "a@t-online.example", "IMAP_PASSWORD": "pw"}
    with pytest.raises(Exception, match="NANOGPT_API_KEY"):
        Settings.from_env(env)
    settings = Settings.from_env(env, require_key=False).with_mock()
    assert settings.use_mock and settings.connector == "imap" and settings.model == "mock"
    assistant = Assistant.from_settings(settings)
    assert isinstance(assistant.llm, MockModel) and not assistant.llm.naive
    assert "NanoGPT" not in help_text(settings) and "MOCK" in help_text(settings)
    real = Settings.from_env({"NANOGPT_API_KEY": "k"})
    assert "NanoGPT" in help_text(real)


def test_cli_mock_flag_rules(monkeypatch, capsys):
    monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="--mock ist für das echte Postfach"):
        cli.main(["--demo", "--mock", "neu"])
    with pytest.raises(SystemExit, match="nur zusammen mit --demo oder --mock"):
        cli.main(["--naiv", "neu"])
    with pytest.raises(SystemExit, match="mit --mock ohne Modell testen"):
        cli.main(["check"])
