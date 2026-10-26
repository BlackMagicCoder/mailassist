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
    mock = demo or settings.use_mock
    model_label = "Modell (Mock, lokal)" if mock else "Bedrock"
    if demo:
        config = f"Demo-Modus: kein Schlüssel nötig, Daten unter {settings.data_dir}"
    elif mock:
        config = "Mock-Modus: echtes Postfach, lokales Regel-Modell – kein Schlüssel, nichts geht an ein Sprachmodell"
    else:
        config = f"Modell {settings.model}, Region {settings.bedrock_region}, Bedrock-Schlüssel gesetzt"
    results = [Check("Konfiguration", True, config)]

    entries, skipped = load_knowledge(settings.knowledge_dir)
    info = f"{len(entries)} freigegebene Einträge in {settings.knowledge_dir}"
    if skipped:
        info += f", {len(skipped)} übersprungen: " + "; ".join(skipped)
    results.append(Check("Wissensbasis", True, info if entries or skipped else f"keine (Ordner {settings.knowledge_dir} leer oder fehlt)"))

    try:
        async with mail_session() as mail:
            found = await mail.search(settings.inbox_folder, None, 1)
            folders = ""
            if hasattr(mail, "sent_folder"):  # IMAP: show what was detected, the usual stumbling block
                drafts = getattr(mail, "drafts_folder", None) or "NICHT GEFUNDEN – IMAP_DRAFTS_FOLDER setzen"
                folders = f"; Entwürfe: {drafts}; Gesendet: {mail.sent_folder or 'nicht gefunden (Verlauf ohne eigene Antworten)'}"
        ok = not folders or "NICHT GEFUNDEN" not in folders
        results.append(Check(mailbox, ok, f"erreichbar, {settings.inbox_folder} lesbar ({len(found)} Testabruf){folders}"))
    except FileNotFoundError:
        results.append(Check(mailbox, False, f"'{settings.baryon_command}' nicht gefunden"))
    except AssistantError as exc:
        # check runs in the local terminal only, so the technical detail (e.g. certificate error) may be shown
        results.append(Check(mailbox, False, f"{exc} ({exc.detail})" if exc.detail else str(exc)))
    except Exception as exc:  # noqa: BLE001 - report any startup failure as a red light
        results.append(Check(mailbox, False, f"Start fehlgeschlagen: {type(exc).__name__}"))

    if llm is not None:
        try:
            completion = await llm.complete("Answer with the single word OK.", "Ping")
            results.append(Check(model_label, True, f"antwortet, Modell {completion.model}"))
        except AssistantError as exc:
            # "Ping" carries no mail content, so the provider's reason (e.g. missing AWS use-case form) may be shown
            results.append(Check(model_label, False, f"{exc} ({exc.detail})" if exc.detail else str(exc)))
    else:
        skipped = "lokal, keine Prüfung nötig" if mock else "nicht geprüft (mit --llm prüfen; kostet einen kleinen Modellaufruf)"
        results.append(Check(model_label, True, skipped))

    if telegram_probe is not None:
        try:
            results.append(Check("Telegram", True, f"Bot {await telegram_probe()} erreichbar"))
        except Exception as exc:  # noqa: BLE001
            results.append(Check("Telegram", False, f"nicht erreichbar: {type(exc).__name__}"))
    return results


def format_checks(results: list[Check]) -> str:
    return "\n".join(f"{'✓' if r.ok else '✗'} {r.name}: {r.info}" for r in results)
