"""`mailassist check`: is everything reachable? Shows status only, never mail content."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config import Settings
from .core import MailSession
from .errors import AssistantError
from .knowledge import load_knowledge
from .llm import LLMAdapter


MAILBOX_LABELS = {
    "baryon": "Postfach (Baryon/Bridge)",
    "demo": "Postfach (Demo, synthetisch)",
    "imap": "Postfach (IMAP)",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    info: str


async def run_checks(
    settings: Settings,
    mail_session: MailSession,
    llm: LLMAdapter | None = None,
    telegram_probe: Callable[[], Awaitable[str]] | None = None,
) -> list[Check]:
    demo = settings.connector == "demo"
    mailbox = MAILBOX_LABELS.get(settings.connector, f"Postfach ({settings.connector})")
    model_label = "Modell (Mock, lokal)" if demo else "NanoGPT"
    config = (f"Demo-Modus: kein Schlüssel nötig, Daten unter {settings.data_dir}" if demo
              else f"Modell {settings.model}, NanoGPT-Schlüssel gesetzt")
    results = [Check("Konfiguration", True, config)]

    entries, skipped = load_knowledge(settings.knowledge_dir)
    info = f"{len(entries)} freigegebene Einträge in {settings.knowledge_dir}"
    if skipped:
        info += f", {len(skipped)} übersprungen: " + "; ".join(skipped)
    results.append(Check("Wissensbasis", True, info if entries or skipped else f"keine (Ordner {settings.knowledge_dir} leer oder fehlt)"))

    try:
        async with mail_session() as mail:
            found = await mail.search(settings.inbox_folder, None, 1)
        results.append(Check(mailbox, True, f"erreichbar, {settings.inbox_folder} lesbar ({len(found)} Testabruf)"))
    except FileNotFoundError:
        results.append(Check(mailbox, False, f"'{settings.baryon_command}' nicht gefunden"))
    except AssistantError as exc:
        results.append(Check(mailbox, False, str(exc)))
    except Exception as exc:  # noqa: BLE001 - report any startup failure as a red light
        results.append(Check(mailbox, False, f"Start fehlgeschlagen: {type(exc).__name__}"))

    if llm is not None:
        try:
            completion = await llm.complete("Answer with the single word OK.", "Ping")
            results.append(Check(model_label, True, f"antwortet, Modell {completion.model}"))
        except AssistantError as exc:
            results.append(Check(model_label, False, str(exc)))
    else:
        results.append(Check(model_label, True, "nicht geprüft (mit --llm prüfen; kostet einen kleinen Modellaufruf)"))

    if telegram_probe is not None:
        try:
            results.append(Check("Telegram", True, f"Bot {await telegram_probe()} erreichbar"))
        except Exception as exc:  # noqa: BLE001
            results.append(Check("Telegram", False, f"nicht erreichbar: {type(exc).__name__}"))
    return results


def format_checks(results: list[Check]) -> str:
    return "\n".join(f"{'✓' if r.ok else '✗'} {r.name}: {r.info}" for r in results)
