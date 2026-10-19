"""Logging setup. Log calls in this package record only ids, sizes and model names,
never mail content. As a second line of defence every record is scrubbed of secrets."""

from __future__ import annotations

import logging
from collections.abc import Iterable


class SecretRedactor(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def setup_logging(secrets: Iterable[str], level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretRedactor(secrets))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # httpx logs every request URL at INFO; Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "telegram", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
