"""Mail access through Baryon MCP (baryon-mcp over stdio -> Proton Mail Bridge).

Only the tools in ALLOWED_TOOLS can be called. Baryon itself has no send tool;
this allowlist additionally keeps draft replacement (save_draft) and attachment
tools out of reach.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

log = logging.getLogger(__name__)

ALLOWED_TOOLS = frozenset({"search_emails", "get_email", "get_thread", "save_reply_draft"})

# Environment passed to baryon-mcp besides the MCP SDK's safe defaults. The
# NanoGPT key and Telegram token are deliberately not inherited.
_PASSTHROUGH_PREFIXES = ("PROTON_BRIDGE_", "BARYON_")
_PASSTHROUGH_VARS = ("XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "APPDATA", "LOCALAPPDATA")


class MailError(Exception):
    pass


@dataclass(frozen=True)
class MessageRef:
    folder: str
    uid: int
    uidvalidity: int

    def as_args(self) -> dict[str, Any]:
        return {"folder": self.folder, "uid": self.uid, "uidvalidity": self.uidvalidity}


@dataclass(frozen=True)
class EmailSummary:
    ref: MessageRef
    subject: str
    sender: str
    date: str


@dataclass(frozen=True)
class Email:
    ref: MessageRef
    subject: str
    sender: str
    to: list[str]
    date: str
    body: str
    truncated: bool = False


@dataclass(frozen=True)
class ThreadMessage:
    uid: int
    subject: str
    sender: str
    date: str
    body: str
    truncated: bool = False


@dataclass(frozen=True)
class SavedDraft:
    subject: str
    sender: str
    to: list[str]
    cc: list[str] = field(default_factory=list)


class MailBackend(Protocol):
    async def search(self, folder: str, query: str | None, limit: int) -> list[EmailSummary]: ...
    async def get_email(self, ref: MessageRef) -> Email: ...
    async def get_thread(self, ref: MessageRef) -> list[ThreadMessage]: ...
    async def save_reply_draft(self, ref: MessageRef, body: str) -> SavedDraft: ...


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", markup)
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
    """MailBackend backed by one baryon-mcp process for the lifetime of `async with`."""

    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None):
        self._params = StdioServerParameters(
            command=command, args=args or [], env=env if env is not None else baryon_environment()
        )
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> BaryonMail:
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(self._params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
        except BaseException:
            await stack.aclose()
            raise
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

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in ALLOWED_TOOLS:
            raise PermissionError(f"Werkzeug {tool!r} ist nicht freigegeben")
        if self._session is None:
            raise MailError("Keine aktive Baryon-Sitzung")
        result = await self._session.call_tool(tool, args)
        texts = [c.text for c in (getattr(result, "content", None) or []) if getattr(c, "type", "") == "text"]
        if getattr(result, "is_error", False):
            # Baryon error texts describe the failure, not mail content.
            raise MailError(f"{tool}: {' '.join(texts)[:500]}")
        data = getattr(result, "structured_content", None)
        if data is None:
            try:
                data = json.loads("".join(texts))
            except ValueError:
                raise MailError(f"{tool}: unerwartetes Antwortformat") from None
        log.info("baryon %s ok", tool)
        return data

    async def search(self, folder: str, query: str | None, limit: int = 10) -> list[EmailSummary]:
        args: dict[str, Any] = {"folder": folder, "limit": limit}
        if query:
            args["query"] = query
        data = await self._call("search_emails", args)
        uidvalidity = data["uidvalidity"]
        return [
            EmailSummary(
                ref=MessageRef(data["folder"], e["uid"], uidvalidity),
                subject=e.get("subject", ""),
                sender=_first(e.get("from")),
                date=e.get("date", ""),
            )
            for e in data.get("emails", [])
        ]

    async def get_email(self, ref: MessageRef) -> Email:
        d = await self._call("get_email", ref.as_args())
        body = d.get("plain_body") or html_to_text(d.get("html_body", ""))
        return Email(
            ref=ref,
            subject=d.get("subject", ""),
            sender=_first(d.get("reply_to")) or _first(d.get("from")),
            to=list(d.get("to") or []),
            date=d.get("date", ""),
            body=body,
            truncated=bool(d.get("text_truncated") or d.get("html_truncated")),
        )

    async def get_thread(self, ref: MessageRef) -> list[ThreadMessage]:
        d = await self._call("get_thread", {**ref.as_args(), "include_bodies": True})
        return [
            ThreadMessage(
                uid=m["uid"],
                subject=m.get("subject", ""),
                sender=_first(m.get("from")),
                date=m.get("date", ""),
                body=m.get("body", ""),
                truncated=bool(m.get("body_truncated")),
            )
            for m in d.get("messages", [])
        ]

    async def save_reply_draft(self, ref: MessageRef, body: str) -> SavedDraft:
        d = await self._call("save_reply_draft", {**ref.as_args(), "text_body": body})
        log.info("draft saved uid=%s chars=%d", d.get("uid"), len(body))
        return SavedDraft(
            subject=d.get("subject", ""),
            sender=d.get("from", ""),
            to=list(d.get("to") or []),
            cc=list(d.get("cc") or []),
        )
