"""Settings loaded from environment variables (optionally via a local .env file)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_MODEL = "anthropic/claude-sonnet-latest"
DEFAULT_BASE_URL = "https://api.nano-gpt.com/api/v1"


class ConfigError(Exception):
    pass


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    # Secrets are excluded from repr so they never end up in tracebacks or logs.
    nanogpt_api_key: str = field(repr=False)
    telegram_token: str | None = field(default=None, repr=False)
    telegram_allowed_user_ids: frozenset[int] = frozenset()
    model: str = DEFAULT_MODEL
    nanogpt_base_url: str = DEFAULT_BASE_URL
    max_tokens: int = 2000
    baryon_command: str = "baryon-mcp"
    inbox_folder: str = "INBOX"
    search_folder: str = "All Mail"
    own_addresses: frozenset[str] = frozenset()
    signature: str | None = None
    data_dir: str = "data"
    knowledge_dir: str = "knowledge"
    # Baryon version whose source was reviewed; empty string disables the check.
    baryon_version: str = "0.6.3"
    # Mail connector: "baryon" (Proton via Bridge), "imap" (T-Online, GMX, …) or "demo" (synthetic + mock model).
    connector: str = "baryon"
    demo_model: str = "vorsichtig"  # mock model variant: "vorsichtig" or "naiv" (obeys injections on purpose)
    # --mock: real mailbox, but the local rule-based MockModel instead of NanoGPT (no key, no model provider).
    use_mock: bool = False
    imap_host: str = "secureimap.t-online.de"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = field(default="", repr=False)
    imap_drafts_folder: str | None = None
    imap_sent_folder: str | None = None

    @classmethod
    def for_demo(cls, env: Mapping[str, str] | None = None, naive: bool = False) -> Settings:
        """Demo settings: no API key needed, separate data folder, demo knowledge base."""
        from dataclasses import replace

        from .demo import DEMO_OWN_ADDRESSES, DEMO_SIGNATURE

        env = dict(os.environ if env is None else env)
        env["NANOGPT_API_KEY"] = env.get("NANOGPT_API_KEY") or "demo-ohne-schluessel"
        defaults = {
            # Own variable on purpose: MAILASSIST_DATA_DIR of the real setup is never reused by the demo.
            "MAILASSIST_DATA_DIR": env.get("MAILASSIST_DEMO_DATA_DIR") or os.path.join("data", "demo"),
            "MAILASSIST_KNOWLEDGE_DIR": os.path.join("examples", "knowledge"),
            "MAILASSIST_OWN_ADDRESSES": DEMO_OWN_ADDRESSES,
            "MAILASSIST_SIGNATURE": DEMO_SIGNATURE,
        }
        for key, value in defaults.items():
            env[key] = value  # the demo always uses its own data, never the real configuration
        settings = cls.from_env(env)
        return replace(settings, connector="demo", demo_model="naiv" if naive else "vorsichtig", model="mock")

    def with_mock(self, naive: bool = False) -> Settings:
        """Same mailbox, but the local MockModel instead of NanoGPT."""
        from dataclasses import replace

        return replace(self, use_mock=True, model="mock", demo_model="naiv" if naive else "vorsichtig")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, require_key: bool = True) -> Settings:
        env = os.environ if env is None else env
        key = env.get("NANOGPT_API_KEY", "").strip()
        if not key:
            if require_key:
                raise ConfigError("NANOGPT_API_KEY ist nicht gesetzt (siehe .env.example), oder mit --mock ohne Modell testen.")
            key = "mock-ohne-schluessel"
        try:
            user_ids = frozenset(int(v) for v in _split(env.get("TELEGRAM_ALLOWED_USER_IDS")))
            max_tokens = int(env.get("MAILASSIST_MAX_TOKENS", "2000"))
            imap_port = int(env.get("IMAP_PORT", "993"))
        except ValueError as exc:
            raise ConfigError(f"Ungültige Zahl in der Konfiguration: {exc}") from None
        connector = env.get("MAILASSIST_CONNECTOR", "baryon").strip().lower() or "baryon"
        if connector not in ("baryon", "imap"):
            raise ConfigError(f"MAILASSIST_CONNECTOR muss 'baryon' oder 'imap' sein (für die Demo: --demo), nicht {connector!r}.")
        imap_user = env.get("IMAP_USER", "").strip()
        imap_password = env.get("IMAP_PASSWORD", "")
        if connector == "imap" and not (imap_user and imap_password):
            raise ConfigError("Für MAILASSIST_CONNECTOR=imap müssen IMAP_USER und IMAP_PASSWORD gesetzt sein.")
        own = {a.lower() for a in _split(env.get("MAILASSIST_OWN_ADDRESSES"))}
        if connector == "imap" and "@" in imap_user:
            own.add(imap_user.lower())
        # Proton has an "All Mail" folder; plain IMAP servers usually do not.
        default_search = "INBOX" if connector == "imap" else "All Mail"
        return cls(
            connector=connector,
            imap_host=env.get("IMAP_HOST", "secureimap.t-online.de").strip(),
            imap_port=imap_port,
            imap_user=imap_user,
            imap_password=imap_password,
            imap_drafts_folder=env.get("IMAP_DRAFTS_FOLDER", "").strip() or None,
            imap_sent_folder=env.get("IMAP_SENT_FOLDER", "").strip() or None,
            nanogpt_api_key=key,
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", "").strip() or None,
            telegram_allowed_user_ids=user_ids,
            model=env.get("MAILASSIST_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            nanogpt_base_url=env.get("NANOGPT_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            max_tokens=max_tokens,
            baryon_command=env.get("BARYON_COMMAND", "baryon-mcp"),
            inbox_folder=env.get("MAILASSIST_INBOX_FOLDER", "INBOX"),
            search_folder=env.get("MAILASSIST_SEARCH_FOLDER", default_search),
            own_addresses=frozenset(own),
            signature=env.get("MAILASSIST_SIGNATURE", "").replace("\\n", "\n").strip() or None,
            data_dir=env.get("MAILASSIST_DATA_DIR", "data"),
            knowledge_dir=env.get("MAILASSIST_KNOWLEDGE_DIR", "knowledge"),
            baryon_version=env.get("MAILASSIST_BARYON_VERSION", "0.6.3").strip(),
        )

    def secrets(self) -> list[str]:
        return [s for s in (self.nanogpt_api_key, self.telegram_token, self.imap_password) if s]
