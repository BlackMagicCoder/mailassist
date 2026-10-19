"""Mail access through Baryon MCP (baryon-mcp over stdio -> Proton Mail Bridge).

Only the tools in ALLOWED_TOOLS can be called. Baryon itself has no send tool;
this allowlist additionally keeps draft replacement (save_draft) and attachment
tools out of reach. Every Baryon response is validated before use.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from typing import Any, Protocol, TypeVar

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import AssistantError
from .models import Email, EmailSummary, MessageRef, SavedDraft, ThreadMessage

__all__ = [
    "ALLOWED_TOOLS", "BaryonMail", "Email", "EmailSummary", "MailBackend", "MailError",
    "MessageRef", "SavedDraft", "ThreadMessage", "baryon_environment", "html_to_text",
]

log = logging.getLogger(__name__)

ALLOWED_TOOLS = frozenset({"search_emails", "get_email", "get_thread", "save_reply_draft"})

# Environment passed to baryon-mcp besides the MCP SDK's safe defaults. The
# NanoGPT key and Telegram token are deliberately not inherited.
_PASSTHROUGH_PREFIXES = ("PROTON_BRIDGE_", "BARYON_")
_PASSTHROUGH_VARS = ("XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "APPDATA", "LOCALAPPDATA")


class MailError(AssistantError):
    pass


class MailBackend(Protocol):
    async def search(self, folder: str, query: str | None, limit: int) -> list[EmailSummary]: ...
    async def get_email(self, ref: MessageRef) -> Email: ...
    async def get_thread(self, ref: MessageRef) -> list[ThreadMessage]: ...
    async def save_reply_draft(self, ref: MessageRef, body: str) -> SavedDraft: ...


# --- Baryon wire format (only the fields we use; unknown fields are ignored) ----------

class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _Summary(_Wire):
    uid: int
    subject: str = ""
    from_: list[str] = Field(default_factory=list, alias="from")
    date: str = ""
    message_id: str = ""


class _SearchPage(_Wire):
    folder: str
    uidvalidity: int
    emails: list[_Summary] = Field(default_factory=list)


class _EmailOut(_Wire):
    subject: str = ""
    from_: list[str] = Field(default_factory=list, alias="from")
    reply_to: list[str] = Field(default_factory=list)
    to: list[str] = Field(default_factory=list)
    date: str = ""
    message_id: str = ""
    plain_body: str = ""
    html_body: str = ""
    text_truncated: bool = False
    html_truncated: bool = False


class _ThreadMsg(_Wire):
    uid: int
    subject: str = ""
    from_: list[str] = Field(default_factory=list, alias="from")
    date: str = ""
    message_id: str = ""
    body: str = ""
    body_truncated: bool = False


class _Thread(_Wire):
    messages: list[_ThreadMsg] = Field(default_factory=list)


class _ReplyDraft(_Wire):
    uid: int
    subject: str = ""
    from_: str = Field("", alias="from")
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)


W = TypeVar("W", bound=_Wire)


def _parse(model: type[W], data: Any, tool: str) -> W:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # Field locations only; the offending values could be mail content.
        locations = "; ".join(".".join(map(str, e["loc"])) + f": {e['type']}" for e in exc.errors())
        raise MailError(f"Unerwartetes Antwortformat von Baryon ({tool}).", detail=locations[:500]) from None


# --- helpers -------------------------------------------------------------------------

_HIDDEN = re.compile(
    r"""(?is)<(\w+)\b[^>]*style\s*=\s*["'][^"']*"""
    r"""(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d]*[1-9])|opacity\s*:\s*0(?![.\d]*[1-9]))"""
    r"""[^"']*["'][^>]*>.*?</\1\s*>"""
)


def html_to_text(markup: str) -> str:
    """Visible text only. Hidden elements are dropped: the owner cannot see them in
    Proton Mail, so they must not reach the model either (classic injection carrier)."""
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", markup)
    markup = re.sub(r"(?is)<!--.*?-->", "", markup)
    markup = _HIDDEN.sub("", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _first(addresses: list[str] | None) -> str:
    return (addresses or [""])[0]


def baryon_environment() -> dict[str, str]:
    env = get_default_environment()
    for name, value in os.environ.items():
        if name == "BARYON_COMMAND":
            continue
        if name.startswith(_PASSTHROUGH_PREFIXES) or name in _PASSTHROUGH_VARS:
            env[name] = value
    return env


class BaryonMail:
    """MailBackend backed by one baryon-mcp process for the lifetime of `async with`.

    `thread_folder` is where conversations are gathered (Proton: "All Mail"), so a
    thread includes the owner's own replies stored in Sent.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        thread_folder: str | None = None,
        expected_version: str | None = None,
    ):
        self._params = StdioServerParameters(
            command=command, args=args or [], env=env if env is not None else baryon_environment()
        )
        self._thread_folder = thread_folder
        self._expected_version = expected_version
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> BaryonMail:
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(self._params))
            session = await stack.enter_async_context(ClientSession(read, write))
            init = await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
        except BaseException:
            await stack.aclose()
            raise
        version = (init.server_info.version or "").removeprefix("v")
        if self._expected_version and version != self._expected_version.removeprefix("v"):
            await stack.aclose()
            raise MailError(
                f"baryon-mcp meldet Version {version or '(keine)'}, geprüft und freigegeben ist "
                f"{self._expected_version}. Version prüfen oder MAILASSIST_BARYON_VERSION anpassen."
            )
        missing = ALLOWED_TOOLS - tools
        if missing:
            await stack.aclose()
            raise MailError(f"baryon-mcp bietet benötigte Werkzeuge nicht an: {sorted(missing)}")
        self._stack, self._session = stack, session
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack:
            await self._stack.aclose()
        self._stack = self._session = None

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool not in ALLOWED_TOOLS:
            raise PermissionError(f"Werkzeug {tool!r} ist nicht freigegeben")
        if self._session is None:
            raise MailError("Keine aktive Baryon-Sitzung")
        result = await self._session.call_tool(tool, args)
        texts = [c.text for c in (getattr(result, "content", None) or []) if getattr(c, "type", "") == "text"]
        if getattr(result, "is_error", False):
            raise MailError(f"Postfach-Zugriff fehlgeschlagen ({tool}).", detail=" ".join(texts)[:500])
        data = getattr(result, "structured_content", None)
        if data is None:
            try:
                data = json.loads("".join(texts))
            except ValueError:
                raise MailError(f"Unerwartetes Antwortformat von Baryon ({tool}).") from None
        log.info("baryon %s ok", tool)
        return data

    async def search(self, folder: str, query: str | None, limit: int = 10) -> list[EmailSummary]:
        args: dict[str, Any] = {"folder": folder, "limit": limit}
        if query:
            args["query"] = query
        page = _parse(_SearchPage, await self._call("search_emails", args), "search_emails")
        return [
            EmailSummary(
                ref=MessageRef(page.folder, e.uid, page.uidvalidity),
                subject=e.subject,
                sender=_first(e.from_),
                date=e.date,
                message_id=e.message_id,
            )
            for e in page.emails
        ]

    async def get_email(self, ref: MessageRef) -> Email:
        d = _parse(_EmailOut, await self._call("get_email", ref.as_args()), "get_email")
        return Email(
            ref=ref,
            subject=d.subject,
            sender=_first(d.reply_to) or _first(d.from_),
            to=list(d.to),
            date=d.date,
            body=d.plain_body or html_to_text(d.html_body),
            truncated=d.text_truncated or d.html_truncated,
            message_id=d.message_id,
        )

    async def get_thread(self, ref: MessageRef) -> list[ThreadMessage]:
        args: dict[str, Any] = {**ref.as_args(), "include_bodies": True}
        if self._thread_folder:
            args["search_folder"] = self._thread_folder
        thread = _parse(_Thread, await self._call("get_thread", args), "get_thread")
        return [
            ThreadMessage(
                uid=m.uid,
                subject=m.subject,
                sender=_first(m.from_),
                date=m.date,
                body=m.body,
                truncated=m.body_truncated,
                message_id=m.message_id,
            )
            for m in thread.messages
        ]

    async def save_reply_draft(self, ref: MessageRef, body: str) -> SavedDraft:
        args = {**ref.as_args(), "text_body": body}
        d = _parse(_ReplyDraft, await self._call("save_reply_draft", args), "save_reply_draft")
        log.info("draft saved uid=%s chars=%d", d.uid, len(body))
        return SavedDraft(subject=d.subject, sender=d.from_, to=list(d.to), cc=list(d.cc))
