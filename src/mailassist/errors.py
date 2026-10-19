"""Common error base. `str(error)` is safe to show anywhere (Telegram, logs);
`detail` may echo third-party text (server messages) and is shown only in the local CLI."""

from __future__ import annotations


class AssistantError(Exception):
    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail
