"""Generic IMAP connector (e.g. T-Online Freemail, GMX, web.de, Posteo, Gmail with app password).

Same contract as BaryonMail. Safety model, enforced by _GuardedImap - the only
object that ever touches the IMAP connection:
  * folders are opened read-only (EXAMINE), bodies fetched with BODY.PEEK, so
    nothing is marked as read;
  * no STORE, COPY, MOVE, EXPUNGE, DELETE, RENAME, CREATE - those methods do not exist here;
  * APPEND only into the drafts folder and only with the \\Draft flag;
  * there is no SMTP anywhere in mailassist: a draft cannot be sent from here.
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import imaplib
import logging
import re
import ssl
import time
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime
from typing import Any

from .mail import MailError, html_to_text_with_notes
from .models import Email, EmailSummary, MessageRef, SavedDraft, ThreadMessage
from .prompt_utils import addresses

log = logging.getLogger(__name__)

T_ONLINE_HOST = "secureimap.t-online.de"
MAX_BODY_CHARS = 50_000
THREAD_BODY_CHARS = 2_000
MAX_THREAD_IDS = 20
DRAFT_NAMES = ("Drafts", "Entwürfe", "Entwurf", "INBOX.Drafts", "INBOX.Entwürfe")
SENT_NAMES = ("Sent", "Gesendet", "Gesendete Objekte", "Sent Items", "INBOX.Sent", "INBOX.Gesendet")
HEADER_FIELDS = "(UID BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT DATE MESSAGE-ID)])"


# --- IMAP modified UTF-7 (RFC 3501, 5.1.3) ------------------------------------------------

def encode_folder(name: str) -> str:
    out, buffer = [], []

    def flush() -> None:
        if buffer:
            raw = base64.b64encode("".join(buffer).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{raw}-")
            buffer.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buffer.append(ch)
    flush()
    return "".join(out)


def decode_folder(name: str) -> str:
    def part(match: re.Match) -> str:
        raw = match.group(1)
        if not raw:
            return "&"
        raw = raw.replace(",", "/")
        return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", part, name)


def _quote(folder: str) -> str:
    encoded = encode_folder(folder)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- guard ------------------------------------------------------------------------------

class _GuardedImap:
    """The only access to the IMAP connection. Exposes read-only commands plus APPEND to drafts."""

    def __init__(self, conn: Any):
        self._conn = conn
        self.drafts_folder: str | None = None

    @staticmethod
    def _ok(result: tuple[str, list], what: str) -> list:
        typ, data = result
        if typ != "OK":
            detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")[:300]
            raise MailError(f"IMAP-Befehl fehlgeschlagen ({what}).", detail=detail)
        return data

    def login(self, user: str, password: str) -> None:
        try:
            self._ok(self._conn.login(user, password), "LOGIN")
        except imaplib.IMAP4.error as exc:
            raise MailError("IMAP-Anmeldung fehlgeschlagen. Benutzer und Passwort für E-Mail-Programme prüfen.",
                            detail=str(exc)[:300]) from None

    def list_folders(self) -> list[tuple[set[str], str]]:
        folders = []
        for line in self._ok(self._conn.list(), "LIST"):
            if isinstance(line, tuple):  # name sent as literal
                head, name = line[0].decode(errors="replace"), line[1].decode(errors="replace")
            elif isinstance(line, bytes):
                head = line.decode(errors="replace")
                match = re.search(r'\) (?:"[^"]*"|NIL) (.+)$', head)
                name = match.group(1).strip() if match else ""
            else:
                continue
            flags = set(re.findall(r"\\\w+", head.split(")", 1)[0]))
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\") if name.startswith('"') else name
            folders.append((flags, decode_folder(name)))
        return folders

    def examine(self, folder: str) -> int:
        """Open read-only and return UIDVALIDITY."""
        self._ok(self._conn.select(_quote(folder), readonly=True), f"EXAMINE {folder}")
        _, data = self._conn.response("UIDVALIDITY")
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            raise MailError(f"Server lieferte keine UIDVALIDITY für {folder}.") from None

    def uid_search(self, *criteria: str, literal: str | None = None) -> list[int]:
        if literal is not None:
            self._conn.literal = literal.encode("utf-8")
            data = self._ok(self._conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria), "UID SEARCH")
        else:
            data = self._ok(self._conn.uid("SEARCH", *criteria), "UID SEARCH")
        return [int(x) for x in (data[0] or b"").split()]

    def uid_fetch(self, uids: list[int], spec: str) -> dict[int, bytes]:
        upper = spec.upper()
        if "RFC822" in upper or ("BODY[" in upper and "BODY.PEEK[" not in upper):
            raise PermissionError("Nur BODY.PEEK ist erlaubt (nichts als gelesen markieren).")
        if not uids:
            return {}
        data = self._ok(self._conn.uid("FETCH", ",".join(map(str, uids)), spec), "UID FETCH")
        out = {}
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                match = re.search(rb"UID (\d+)", item[0])
                if match:
                    out[int(match.group(1))] = item[1]
        return out

    def append_draft(self, folder: str, message: bytes) -> str:
        if self.drafts_folder is None or folder != self.drafts_folder:
            raise PermissionError("APPEND ist nur in den Entwürfe-Ordner erlaubt.")
        data = self._ok(self._conn.append(_quote(folder), r"(\Draft)", imaplib.Time2Internaldate(time.time()), message),
                        "APPEND")
        return b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")

    def logout(self) -> None:
        try:
            self._conn.logout()
        except Exception:  # noqa: BLE001 - connection may already be gone
            pass


# --- connector ----------------------------------------------------------------------------

def _decoded(msg: email.message.Message, header: str) -> str:
    value = msg.get(header)
    return str(value).strip() if value is not None else ""


def _iso(date_header: str) -> str:
    try:
        return parsedate_to_datetime(date_header).isoformat()
    except (TypeError, ValueError):
        return ""


def _msgid(value: str) -> str:
    return value.strip().strip("<>")


def _parse(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


def _body(msg: email.message.EmailMessage) -> tuple[str, tuple[str, ...]]:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", ()
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        content = part.get_payload(decode=True).decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        return html_to_text_with_notes(content)
    return content.strip(), ()


class ImapMail:
    """MailBackend over IMAPS. One connection per `async with`; all blocking IMAP calls run in a thread."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 993,
        own_address: str | None = None,
        inbox: str = "INBOX",
        drafts_folder: str | None = None,
        sent_folder: str | None = None,
        timeout: float = 30.0,
        connect: Callable[[], Any] | None = None,
    ):
        self._user, self._password = user, password
        self._own = own_address or (user if "@" in user else "")
        self._inbox = inbox
        self._wanted_drafts, self._wanted_sent = drafts_folder, sent_folder
        self._connect = connect or (lambda: imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(),
                                                              timeout=timeout))
        self._imap: _GuardedImap | None = None
        self.sent_folder: str | None = None

    async def _run(self, func: Callable, *args: Any) -> Any:
        return await asyncio.to_thread(func, *args)

    async def __aenter__(self) -> ImapMail:
        try:
            conn = await self._run(self._connect)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError("IMAP-Server nicht erreichbar.", detail=f"{type(exc).__name__}: {exc}"[:300]) from None
        guard = _GuardedImap(conn)
        try:
            await self._run(guard.login, self._user, self._password)
            folders = await self._run(guard.list_folders)
        except BaseException:
            await self._run(guard.logout)
            raise
        guard.drafts_folder = self._pick(folders, self._wanted_drafts, r"\Drafts", DRAFT_NAMES)
        self.sent_folder = self._pick(folders, self._wanted_sent, r"\Sent", SENT_NAMES)
        self._imap = guard
        log.info("imap connected, drafts=%s sent=%s", guard.drafts_folder, self.sent_folder)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._imap is not None:
            await self._run(self._imap.logout)
        self._imap = None

    @property
    def drafts_folder(self) -> str | None:
        return self._imap.drafts_folder if self._imap else None

    @staticmethod
    def _pick(folders: list[tuple[set[str], str]], wanted: str | None, special: str, names: tuple[str, ...]) -> str | None:
        existing = {name for _, name in folders}
        if wanted:
            if wanted not in existing:
                raise MailError(f"Ordner „{wanted}“ gibt es auf dem Server nicht. Vorhanden: {', '.join(sorted(existing))}")
            return wanted
        for flags, name in folders:
            if special in flags:
                return name
        lowered = {name.casefold(): name for name in existing}
        return next((lowered[n.casefold()] for n in names if n.casefold() in lowered), None)

    def _guard(self) -> _GuardedImap:
        if self._imap is None:
            raise MailError("Keine aktive IMAP-Sitzung")
        return self._imap

    # --- MailBackend ---------------------------------------------------------------

    async def search(self, folder: str, query: str | None, limit: int = 10) -> list[EmailSummary]:
        imap = self._guard()

        def work() -> list[EmailSummary]:
            uidvalidity = imap.examine(folder)
            uids = imap.uid_search("TEXT", literal=query) if query else imap.uid_search("ALL")
            chosen = sorted(uids)[-limit:]
            headers = imap.uid_fetch(chosen, HEADER_FIELDS)
            out = []
            for uid in sorted(headers, reverse=True):
                msg = _parse(headers[uid])
                out.append(EmailSummary(MessageRef(folder, uid, uidvalidity), _decoded(msg, "Subject"),
                                        _decoded(msg, "From"), _iso(_decoded(msg, "Date")),
                                        _msgid(_decoded(msg, "Message-ID"))))
            return out

        return await self._run(work)

    def _fetch_full(self, ref: MessageRef) -> email.message.EmailMessage:
        imap = self._guard()
        if imap.examine(ref.folder) != ref.uidvalidity:
            raise MailError("Ordner wurde auf dem Server neu aufgebaut – bitte die Liste neu laden.")
        raw = imap.uid_fetch([ref.uid], "(UID BODY.PEEK[])").get(ref.uid)
        if raw is None:
            raise MailError(f"Nachricht {ref.uid} in {ref.folder} nicht gefunden.")
        return _parse(raw)

    async def get_email(self, ref: MessageRef) -> Email:
        def work() -> Email:
            msg = self._fetch_full(ref)
            body, notes = _body(msg)
            return Email(
                ref=ref, subject=_decoded(msg, "Subject"),
                sender=_decoded(msg, "Reply-To") or _decoded(msg, "From"),
                to=[a for _, a in getaddresses([_decoded(msg, "To")]) if a], date=_iso(_decoded(msg, "Date")),
                body=body[:MAX_BODY_CHARS], truncated=len(body) > MAX_BODY_CHARS,
                message_id=_msgid(_decoded(msg, "Message-ID")), notes=notes,
            )

        return await self._run(work)

    async def get_thread(self, ref: MessageRef) -> list[ThreadMessage]:
        """Messages named in References/In-Reply-To, looked up in INBOX and Sent (IMAP has no portable threads)."""
        imap = self._guard()

        def work() -> list[ThreadMessage]:
            selected = self._fetch_full(ref)
            ids = re.findall(r"<([^>]+)>", f"{_decoded(selected, 'References')} {_decoded(selected, 'In-Reply-To')}")
            # IDs are sender-controlled: skip anything that could break out of the quoted search string.
            ids = [i for i in dict.fromkeys(ids) if not re.search(r'["\\\r\n]', i)][-MAX_THREAD_IDS:]
            messages: dict[str, ThreadMessage] = {}
            own_id = _msgid(_decoded(selected, "Message-ID"))
            body, _ = _body(selected)
            messages[own_id or "selected"] = ThreadMessage(ref.uid, _decoded(selected, "Subject"), _decoded(selected, "From"),
                                                           _iso(_decoded(selected, "Date")), body[:THREAD_BODY_CHARS],
                                                           len(body) > THREAD_BODY_CHARS, own_id)
            for folder in dict.fromkeys(f for f in (self._inbox, self.sent_folder) if f):
                if not ids:
                    break
                imap.examine(folder)
                for msg_id in ids:
                    if msg_id in messages:
                        continue
                    for uid in imap.uid_search("HEADER", "Message-ID", f'"<{msg_id}>"')[:1]:
                        raw = imap.uid_fetch([uid], "(UID BODY.PEEK[])").get(uid)
                        if raw is None:
                            continue
                        msg = _parse(raw)
                        text, _ = _body(msg)
                        messages[msg_id] = ThreadMessage(uid, _decoded(msg, "Subject"), _decoded(msg, "From"),
                                                         _iso(_decoded(msg, "Date")), text[:THREAD_BODY_CHARS],
                                                         len(text) > THREAD_BODY_CHARS, msg_id)
            return sorted(messages.values(), key=lambda m: m.date)

        return await self._run(work)

    async def save_reply_draft(self, ref: MessageRef, body: str) -> SavedDraft:
        imap = self._guard()
        if imap.drafts_folder is None:
            raise MailError("Kein Entwürfe-Ordner gefunden. Bitte IMAP_DRAFTS_FOLDER setzen (siehe `mailassist check`).")

        def work() -> SavedDraft:
            original = self._fetch_full(ref)
            own = addresses(self._own) if self._own else set()
            reply_to = _decoded(original, "Reply-To") or _decoded(original, "From")
            recipients = [f"{n} <{a}>" if n else a for n, a in getaddresses([reply_to]) if a and a.lower() not in own]
            subject = _decoded(original, "Subject")
            subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            parent = _decoded(original, "Message-ID")
            references = " ".join(x for x in (_decoded(original, "References"), parent) if x)

            draft = EmailMessage(policy=email.policy.SMTP)
            draft["From"] = self._own or self._user
            draft["To"] = ", ".join(recipients)
            draft["Subject"] = subject
            if parent:
                draft["In-Reply-To"] = parent
                draft["References"] = references
            draft["Date"] = email.utils.formatdate(localtime=True)
            domain = (self._own or self._user).rpartition("@")[2] or None
            draft["Message-ID"] = make_msgid(domain=domain)
            draft.set_content(body)
            imap.append_draft(imap.drafts_folder, draft.as_bytes())
            log.info("imap draft appended chars=%d", len(body))
            return SavedDraft(subject=subject, sender=draft["From"], to=recipients)

        return await self._run(work)
