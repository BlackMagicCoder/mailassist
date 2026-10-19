"""Telegram front end. Only user ids in TELEGRAM_ALLOWED_USER_IDS are served;
every other update is ignored. The bot can save drafts but never send mail.

Draft buttons carry the proposal id and a content-hash prefix, so an approval
always refers to exactly the text shown above it. Long drafts are shown in full,
split over several messages, with the buttons under the last part only.
"""

from __future__ import annotations

import logging
import traceback

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .approval import MIN_HASH_PREFIX
from .config import ConfigError, Settings
from .core import Assistant, DraftProposal
from .errors import AssistantError
from .models import EmailSummary

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3800
HELP = (
    "Befehle:\n"
    "/neu – neueste Nachrichten im Posteingang\n"
    "/suche TEXT – Postfach durchsuchen\n"
    "Dann eine Nachricht antippen und schreiben, was die Antwort sagen soll, z. B. „freundliche Absage“.\n\n"
    "Ich speichere Entwürfe nur auf deinen Knopfdruck und sende nie selbst. "
    "Der Inhalt der gewählten Unterhaltung wird dafür an NanoGPT/Anthropic übertragen."
)


def _owner(update: Update) -> str:
    return f"telegram:{update.effective_user.id}"


def _assistant(context: ContextTypes.DEFAULT_TYPE) -> Assistant:
    return context.bot_data["assistant"]


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split at paragraph, then line boundaries; hard-cut only lines longer than limit."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current or not parts:
        parts.append(current)
    if len(parts) > 1:
        parts = [f"(Teil {i}/{len(parts)})\n{p}" for i, p in enumerate(parts, 1)]
    return parts


def draft_buttons(proposal: DraftProposal) -> InlineKeyboardMarkup:
    h = proposal.content_hash[:MIN_HASH_PREFIX]
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Speichern", callback_data=f"save:{proposal.id}:{h}"),
            InlineKeyboardButton("Ändern", callback_data=f"edit:{proposal.id}"),
            InlineKeyboardButton("Verwerfen", callback_data=f"drop:{proposal.id}"),
        ]]
    )


def _results_markup(results: list[EmailSummary]) -> InlineKeyboardMarkup:
    rows = []
    for i, r in enumerate(results):
        label = f"{r.sender.split('<')[0].strip()[:25]} – {r.subject[:35]}"
        rows.append([InlineKeyboardButton(label or "(ohne Betreff)", callback_data=f"sel:{i}")])
    return InlineKeyboardMarkup(rows)


async def _show_results(update: Update, context: ContextTypes.DEFAULT_TYPE, results: list[EmailSummary]) -> None:
    context.user_data["results"] = results
    context.user_data["state"] = None
    if not results:
        await update.effective_message.reply_text("Keine Nachrichten gefunden.")
        return
    await update.effective_message.reply_text("Welche Nachricht?", reply_markup=_results_markup(results))


async def _retire_buttons(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the buttons under the previous draft (best effort; the gate refuses them anyway)."""
    previous = context.user_data.pop("draft_message", None)
    if previous is None:
        return
    try:
        await previous.edit_reply_markup(reply_markup=None)
    except (TelegramError, AttributeError):
        pass


async def _send_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE, proposal: DraftProposal) -> None:
    await _retire_buttons(context)
    context.user_data["proposal_id"] = proposal.id
    context.user_data["state"] = None
    header = f"Entwurf an {proposal.recipient}\n{proposal.reply_subject}\n\n"
    notes = []
    if proposal.todo_count:
        notes.append(f"⚠️ {proposal.todo_count} offene [[TODO]]-Stelle(n)")
    notes.extend(f"⚠️ {w}" for w in proposal.warnings)
    footer = ("\n\n" + "\n".join(notes)) if notes else ""
    parts = split_message(header + proposal.body + footer)
    # Plain text on purpose: no parse_mode, so mail content cannot inject formatting or links.
    for part in parts[:-1]:
        await update.effective_message.reply_text(part)
    last = await update.effective_message.reply_text(parts[-1], reply_markup=draft_buttons(proposal))
    context.user_data["draft_message"] = last


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP)


async def cmd_neu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_results(update, context, await _assistant(context).recent())


async def cmd_suche(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args or []).strip()
    if not query:
        await update.effective_message.reply_text("Bitte Suchbegriff angeben, z. B. /suche Angebot Müller")
        return
    await _show_results(update, context, await _assistant(context).search(query))


async def _select(update: Update, context: ContextTypes.DEFAULT_TYPE, index_text: str) -> None:
    message = update.callback_query.message
    results = context.user_data.get("results") or []
    if not index_text.isdigit() or int(index_text) >= len(results):
        await message.reply_text("Diese Liste ist veraltet. Bitte /neu oder /suche erneut ausführen.")
        return
    selected = results[int(index_text)]
    context.user_data["selected"] = selected
    context.user_data["state"] = "await_instruction"
    await message.reply_text(
        f"Ausgewählt: {selected.subject}\nWas soll die Antwort sagen? (z. B. „freundliche Absage“, "
        "oder „-“ für einen allgemeinen Vorschlag)"
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    kind, _, rest = (query.data or "").partition(":")
    if kind == "sel":
        await _select(update, context, rest)
        return
    assistant, owner = _assistant(context), _owner(update)
    proposal_id, _, content_hash = rest.partition(":")
    if kind == "save":
        await query.edit_message_reply_markup(reply_markup=None)
        saved = await assistant.save_approved(proposal_id, content_hash, owner)
        context.user_data.pop("draft_message", None)
        await query.message.reply_text(
            f"✅ Entwurf gespeichert: „{saved.subject}“ an {', '.join(saved.to)}.\n"
            "Bitte in Proton Mail öffnen, prüfen und selbst senden."
        )
    elif kind == "edit":
        assistant.current_proposal(proposal_id, owner)  # refuses outdated drafts
        context.user_data["proposal_id"] = proposal_id
        context.user_data["state"] = "await_change"
        await query.message.reply_text("Was soll geändert werden?")
    elif kind == "drop":
        await query.edit_message_reply_markup(reply_markup=None)
        assistant.discard(proposal_id, owner)
        context.user_data.pop("draft_message", None)
        await query.message.reply_text("Verworfen, nichts gespeichert.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    state = context.user_data.get("state")
    assistant, owner = _assistant(context), _owner(update)
    if state == "await_instruction":
        selected: EmailSummary = context.user_data["selected"]
        await update.effective_message.reply_text("Erstelle Entwurf …")
        instruction = None if text in ("", "-") else text
        context.user_data["instruction"] = instruction
        proposal = await assistant.propose(selected.ref, instruction, owner=owner)
        await _send_proposal(update, context, proposal)
    elif state == "await_change" and context.user_data.get("proposal_id"):
        previous = assistant.current_proposal(context.user_data["proposal_id"], owner)
        await update.effective_message.reply_text("Überarbeite Entwurf …")
        proposal = await assistant.propose(
            previous.ref, context.user_data.get("instruction"), previous=previous, change_request=text, owner=owner
        )
        await _send_proposal(update, context, proposal)
    else:
        await update.effective_message.reply_text(HELP)


def user_error_text(error: BaseException | None) -> str:
    if isinstance(error, AssistantError):
        # Only the safe message; `detail` may echo third-party text and Telegram is not end-to-end encrypted.
        return f"Fehler: {error}"
    # Code locations only, no exception message: it could contain mail content.
    frames = "".join(traceback.format_tb(error.__traceback__)) if error else ""
    log.error("unhandled %s\n%s", type(error).__name__, frames)
    return "Unerwarteter Fehler. Details stehen im Server-Log."


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = user_error_text(context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(message)


async def _gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler; drops updates from anyone not allowlisted."""
    user = update.effective_user if isinstance(update, Update) else None
    if user is None or user.id not in context.bot_data["allowed_user_ids"]:
        log.warning("ignored update from user id=%s", user.id if user else None)
        raise ApplicationHandlerStop


def build_application(settings: Settings, assistant: Assistant | None = None) -> Application:
    if not settings.telegram_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN ist nicht gesetzt.")
    if not settings.telegram_allowed_user_ids:
        raise ConfigError("TELEGRAM_ALLOWED_USER_IDS ist leer – der Bot würde niemanden bedienen.")
    app = Application.builder().token(settings.telegram_token).build()
    app.bot_data["assistant"] = assistant or Assistant.from_settings(settings)
    app.bot_data["allowed_user_ids"] = settings.telegram_allowed_user_ids
    app.add_handler(TypeHandler(Update, _gate), group=-1)
    allowed = filters.User(user_id=list(settings.telegram_allowed_user_ids))
    app.add_handler(CommandHandler(["start", "hilfe", "help"], cmd_start, filters=allowed))
    app.add_handler(CommandHandler("neu", cmd_neu, filters=allowed))
    app.add_handler(CommandHandler("suche", cmd_suche, filters=allowed))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def run_bot(settings: Settings) -> None:
    app = build_application(settings)
    log.info("bot started, allowed users=%d, model=%s", len(settings.telegram_allowed_user_ids), settings.model)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
