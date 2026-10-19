"""IMAP connector (T-Online & co.): read-only access, drafts only, no sending - against a fake
imaplib client with trap methods. Optional: a real IMAP test server (see bottom)."""

import imaplib
import os
import pathlib
import re
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from mailassist.config import ConfigError, Settings
from mailassist.imap import ImapMail, _GuardedImap, decode_folder, encode_folder
from mailassist.mail import MailError
from mailassist.models import MessageRef

from fake_imap import OWN, FakeIMAP, fetch_specs, header, text_search_used


def connector(fake: FakeIMAP, **kwargs) -> ImapMail:
    return ImapMail("imap.test", OWN, "geheim", connect=lambda: fake, **kwargs)


# --- reading -------------------------------------------------------------------------------

async def test_search_read_and_thread_without_marking_anything_read():
    fake = FakeIMAP()
    async with connector(fake) as mail:
        assert mail.drafts_folder == "Entwürfe" and mail.sent_folder == "Gesendet"
        inbox = await mail.search("INBOX", None, 10)
        assert [s.ref.uid for s in inbox] == [13, 12, 11]
        assert inbox[1].subject == "Re: Angebot FB-200 für uns"  # RFC 2047 decoded
        assert inbox[0].ref.uidvalidity == 777

        hits = await mail.search("INBOX", "Lieferung", 10)
        assert [s.ref.uid for s in hits] == [12] and text_search_used(fake)

        email = await mail.get_email(inbox[1].ref)
        assert email.sender == "Einkauf <einkauf@kunde.example>"  # Reply-To wins
        assert "15.11.2026" in email.body and email.message_id == "a2@example.org"

        thread = await mail.get_thread(inbox[1].ref)
        assert [m.message_id for m in thread] == ["a1@example.org", "s1@example.org", "a2@example.org"]
        assert "4.850 €" in thread[1].body  # own reply found in "Gesendet"

    selects = [c for c in fake.calls if c[0] == "select"]
    assert selects and all(readonly for _, _, readonly in selects)
    assert all("BODY.PEEK[" in spec for spec in fetch_specs(fake))
    assert fake.trap_calls() == [] and fake.appended == []
    assert fake.calls[-1] == ("logout",)


async def test_hidden_html_is_removed_and_noted():
    async with connector(FakeIMAP()) as mail:
        email = await mail.get_email(MessageRef("INBOX", 13, 777))
    assert "Was kostet die Wartung?" in email.body and "gratis" not in email.body
    assert any("versteckte" in note for note in email.notes)


async def test_stale_uidvalidity_is_refused():
    async with connector(FakeIMAP()) as mail:
        with pytest.raises(MailError, match="neu aufgebaut"):
            await mail.get_email(MessageRef("INBOX", 12, 1))


# --- drafts ---------------------------------------------------------------------------------

async def test_reply_draft_goes_to_drafts_with_threading_headers_and_nothing_else():
    fake = FakeIMAP()
    async with connector(fake) as mail:
        saved = await mail.save_reply_draft(MessageRef("INBOX", 12, 777), "Ja, 15.11.2026 klappt.\n\nViele Grüße")
    assert len(fake.appended) == 1
    folder, flags, raw = fake.appended[0]
    assert folder == "Entwürfe" and flags == r"(\Draft)"
    msg = message_from_bytes(raw, policy=default_policy)
    assert str(msg["To"]) == "Einkauf <einkauf@kunde.example>" and str(msg["From"]) == OWN
    assert str(msg["Subject"]) == "Re: Angebot FB-200 für uns"
    assert str(msg["In-Reply-To"]) == "<a2@example.org>"
    assert str(msg["References"]).split() == ["<a1@example.org>", "<s1@example.org>", "<a2@example.org>"]
    assert msg.get_content().strip().startswith("Ja, 15.11.2026 klappt.")
    assert saved.to == ["Einkauf <einkauf@kunde.example>"]
    assert fake.trap_calls() == []
    assert ("append", "Entwürfe", r"(\Draft)") in fake.calls
    assert sum(c[0] == "append" for c in fake.calls) == 1


async def test_own_address_is_never_a_recipient():
    fake = FakeIMAP()
    async with connector(fake) as mail:
        saved = await mail.save_reply_draft(MessageRef("Gesendet", 5, 777), "x")
    assert saved.to == [] and OWN not in header(fake.appended[0][2], "To")


# --- guard ------------------------------------------------------------------------------------

def test_guard_has_no_destructive_methods_and_refuses_unsafe_requests():
    guard = _GuardedImap(FakeIMAP())
    for name in ("store", "expunge", "copy", "move", "delete", "rename", "create", "select", "uid", "send", "sendmail"):
        assert not hasattr(guard, name), name
    with pytest.raises(PermissionError):
        guard.uid_fetch([1], "(UID BODY[])")
    with pytest.raises(PermissionError):
        guard.uid_fetch([1], "(RFC822)")
    guard.drafts_folder = "Entwürfe"
    with pytest.raises(PermissionError):
        guard.append_draft("INBOX", b"x")


def test_no_smtp_anywhere_in_mailassist():
    src = pathlib.Path(__file__).parents[1] / "src" / "mailassist"
    code = "\n".join(p.read_text(encoding="utf-8") for p in src.rglob("*.py"))
    assert "smtplib" not in code and "SMTP(" not in code
    imap_code = (src / "imap.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(store|expunge|copy|move|delete|rename|create)\(", imap_code)


# --- connection, folders, config --------------------------------------------------------------

async def test_login_failure_and_unreachable_server_give_clear_errors():
    with pytest.raises(MailError, match="Passwort für E-Mail-Programme"):
        async with ImapMail("imap.test", OWN, "falsch", connect=lambda: FakeIMAP()):
            pass

    def unreachable():
        raise OSError("connection refused")

    with pytest.raises(MailError, match="nicht erreichbar"):
        async with ImapMail("imap.test", OWN, "geheim", connect=unreachable):
            pass


async def test_folder_detection_by_name_and_explicit_override():
    plain = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "." "Drafts"', b'(\\HasNoChildren) "." "Sent"']
    async with connector(FakeIMAP(list_lines=plain)) as mail:
        assert (mail.drafts_folder, mail.sent_folder) == ("Drafts", "Sent")
    with pytest.raises(MailError, match="gibt es auf dem Server nicht"):
        async with connector(FakeIMAP(), drafts_folder="Vorlagen"):
            pass


def test_modified_utf7_roundtrip():
    for name in ("Entwürfe", "Gesendete Objekte", "A&B", "Ärger/Übersicht"):
        assert decode_folder(encode_folder(name)) == name
    assert encode_folder("Entwürfe") == "Entw&APw-rfe"


def test_imap_settings():
    env = {"NANOGPT_API_KEY": "k", "MAILASSIST_CONNECTOR": "imap", "IMAP_USER": "Kira@T-Online.example",
           "IMAP_PASSWORD": "pw-123"}
    s = Settings.from_env(env)
    assert s.connector == "imap" and s.imap_host == "secureimap.t-online.de" and s.imap_port == 993
    assert s.search_folder == "INBOX" and "kira@t-online.example" in s.own_addresses
    assert "pw-123" in s.secrets() and "pw-123" not in repr(s)
    with pytest.raises(ConfigError, match="IMAP_USER und IMAP_PASSWORD"):
        Settings.from_env({"NANOGPT_API_KEY": "k", "MAILASSIST_CONNECTOR": "imap"})
    with pytest.raises(ConfigError, match="MAILASSIST_CONNECTOR"):
        Settings.from_env({"NANOGPT_API_KEY": "k", "MAILASSIST_CONNECTOR": "smtp"})
    assert Settings.from_env({"NANOGPT_API_KEY": "k"}).search_folder == "All Mail"  # Proton default unchanged


# --- optional: real IMAP test server ------------------------------------------------------------
# MAILASSIST_IMAP_TEST_SERVER=host:port:user:password (plain IMAP, e.g. GreenMail in Docker on 3143).
# Never point this at a real mailbox: the test appends synthetic mails to INBOX and a draft folder.

TEST_SERVER = os.environ.get("MAILASSIST_IMAP_TEST_SERVER")


@pytest.mark.imap_server
@pytest.mark.skipif(not TEST_SERVER, reason="opt-in: MAILASSIST_IMAP_TEST_SERVER=host:port:user:password")
async def test_against_real_imap_test_server():
    host, port, user, password = TEST_SERVER.split(":", 3)

    def connect():
        return imaplib.IMAP4(host, int(port))

    seed = connect()
    seed.login(user, password)
    for name in ("Drafts", "Sent"):
        seed.create(name)  # setup of the TEST server only - mailassist itself can't do this
    from fake_imap import default_folders

    for raw in default_folders()["INBOX"].values():
        seed.append("INBOX", None, imaplib.Time2Internaldate(0), raw)
    seed.logout()

    async with ImapMail(host, user, password, port=int(port), own_address=OWN, connect=connect) as mail:
        found = await mail.search("INBOX", "Lieferung", 10)
        assert found and found[0].subject.startswith("Re: Angebot FB-200")
        email = await mail.get_email(found[0].ref)
        assert "15.11.2026" in email.body
        saved = await mail.save_reply_draft(found[0].ref, "Testentwurf")
        assert saved.subject.startswith("Re:")


async def test_health_check_shows_detected_imap_folders():
    from contextlib import asynccontextmanager

    from mailassist.health import format_checks, run_checks

    settings = Settings.from_env({"NANOGPT_API_KEY": "k", "MAILASSIST_CONNECTOR": "imap", "IMAP_USER": OWN,
                                  "IMAP_PASSWORD": "geheim", "MAILASSIST_KNOWLEDGE_DIR": "tests/_no_knowledge"})

    def session_for(fake):
        @asynccontextmanager
        async def session():
            async with connector(fake) as mail:
                yield mail
        return session

    text = format_checks(await run_checks(settings, session_for(FakeIMAP())))
    assert "✓ Postfach (IMAP)" in text and "Entwürfe: Entwürfe; Gesendet: Gesendet" in text

    no_drafts = [rb'(\HasNoChildren) "/" "INBOX"']
    text = format_checks(await run_checks(settings, session_for(FakeIMAP(list_lines=no_drafts))))
    assert "✗ Postfach (IMAP)" in text and "IMAP_DRAFTS_FOLDER setzen" in text


@pytest.mark.imap_server
@pytest.mark.skipif(not TEST_SERVER, reason="opt-in: MAILASSIST_IMAP_TEST_SERVER=host:port:user:password")
async def test_full_flow_against_real_imap_test_server(tmp_path):
    """Analyse, draft (mock model) and save through the core; then inspect the server independently."""
    from contextlib import asynccontextmanager

    from mailassist.core import Assistant
    from mailassist.dataflow import DataFlow
    from fake_imap import default_folders

    host, port, user, password = TEST_SERVER.split(":", 3)

    def connect():
        return imaplib.IMAP4(host, int(port))

    marker = f"flow-{os.getpid()}"
    seed = connect()
    seed.login(user, password)
    for name in ("Drafts", "Sent"):
        seed.create(name)
    for folder, mails in (("INBOX", default_folders()["INBOX"]), ("Sent", default_folders()["Gesendet"])):
        for raw in mails.values():
            seed.append(folder, None, imaplib.Time2Internaldate(0), raw.replace(b"FB-200", f"FB-200 {marker}".encode()))
    seed.logout()

    @asynccontextmanager
    async def session():
        async with ImapMail(host, user, password, port=int(port), own_address=OWN, connect=connect) as mail:
            yield mail

    settings = Settings.from_env({"MAILASSIST_CONNECTOR": "imap", "IMAP_USER": OWN, "IMAP_PASSWORD": password,
                                  "MAILASSIST_KNOWLEDGE_DIR": "examples/knowledge",
                                  "MAILASSIST_DATA_DIR": str(tmp_path)}, require_key=False).with_mock()
    from mailassist.audit import AuditLog, DraftLedger
    from mailassist.demo import MockModel

    assistant = Assistant(settings, session, MockModel(), audit=AuditLog(tmp_path / "a.jsonl"),
                          ledger=DraftLedger(tmp_path / "l.sqlite"))
    assistant.tracer = flow = DataFlow()
    hit = (await assistant.search("Lieferung bis 15.11.2026", 20))[0]
    triage, _ = await assistant.analyse(hit.ref)
    proposal = await assistant.propose(hit.ref, "Liefertermin bestätigen")
    saved = await assistant.save(proposal)
    assert saved.subject.startswith("Re:") and any("4.850" in str(d) for _, d in flow.steps)  # own reply from Sent in thread

    check = connect()
    check.login(user, password)
    check.select("Drafts", readonly=True)
    _, uids = check.uid("SEARCH", "ALL")
    _, data = check.uid("FETCH", uids[0].split()[-1].decode(), "(FLAGS BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO TO)])")
    assert rb"\Draft" in data[0][0] and b"In-Reply-To: <a2@example.org>" in data[0][1]
    check.select("INBOX", readonly=True)
    _, unseen = check.uid("SEARCH", "UNSEEN")
    _, everything = check.uid("SEARCH", "ALL")
    assert unseen[0].split() == everything[0].split()  # nothing was marked as read
    _, deleted = check.uid("SEARCH", "DELETED")
    assert deleted[0] == b""
    check.logout()
