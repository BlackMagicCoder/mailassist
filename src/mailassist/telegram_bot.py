"""Telegram front end. Only user ids in TELEGRAM_ALLOWED_USER_IDS are served;
every other update is ignored. The bot can save drafts but never send mail."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from .config import ConfigError, Settings
from .core import Assistant, DraftProposal
from .llm import LLMError
from .mail import EmailSummary, MailError

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
HELP = (
    "Befehle:\n"
    "/neu – neueste Nachrichten im Posteingang\n"
    "/suche TEXT – Postfach durchsuchen\n"
    "Dann eine Nachricht antippen und schreiben, was die Antwort sagen soll, z. B. „freundliche Absage“.\n\n"
    "Ich speichere Entwürfe nur auf deinen Knopfdruck und sende nie selbst. "
    "Der Inhalt der gewählten Unterhaltung wird dafür an NanoGPT/Anthropic übertragen."
)
DRAFT_BUTTONS = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("Speichern", callback_data="save"),
        InlineKeyboardButton("Ändern", callback_data="edit"),
        InlineKeyboardButton("Verwerfen", callback_data="drop"),
    ]]
)


def _assistant(context: ContextTypes.DEFAULT_TYPE) -> Assistant:
    return context.bot_data["assistant"]


def _clip(text: str, reserve: int = 200) -> str:
    limit = TELEGRAM_LIMIT - reserve
    return text if len(text) <= limit else text[:limit] + "\n[… gekürzt, vollständig im gespeicherten Entwurf]"


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


async def _send_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE, proposal: DraftProposal) -> None:
    context.user_data["proposal"] = proposal
    context.user_data["state"] = None
    header = f"Entwurf an {proposal.recipient}\n{proposal.reply_subject}\n\n"
    footer = f"\n\n⚠️ {proposal.todo_count} offene [[TODO]]-Stelle(n)" if proposal.todo_count else ""
    # Plain text on purpose: no parse_mode, so mail content cannot inject formatting or links.
    await update.effective_message.reply_text(header + _clip(proposal.body) + footer, reply_markup=DRAFT_BUTTONS)


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


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("sel:"):
        results = context.user_data.get("results") or []
        index = int(data[4:])
        if index >= len(results):
            await query.message.reply_text("Diese Liste ist veraltet. Bitte /neu oder /suche erneut ausführen.")
            return
        context.user_data["selected"] = results[index]
        context.user_data["state"] = "await_instruction"
        await query.message.reply_text(
            f"Ausgewählt: {results[index].subject}\nWas soll die Antwort sagen? (z. B. „freundliche Absage“, "
            "oder „-“ für einen allgemeinen Vorschlag)"
        )
        return
    proposal: DraftProposal | None = context.user_data.get("proposal")
    if proposal is None:
        await query.message.reply_text("Kein offener Entwurf.")
        return
    if data == "save":
        context.user_data["proposal"] = None
        await query.edit_message_reply_markup(reply_markup=None)
        saved = await _assistant(context).save(proposal)
        await query.message.reply_text(
            f"✅ Entwurf gespeichert: „{saved.subject}“ an {', '.join(saved.to)}.\n"
            "Bitte in Proton Mail öffnen, prüfen und selbst senden."
        )
    elif data == "edit":
        context.user_data["state"] = "await_change"
        await query.message.reply_text("Was soll geändert werden?")
    elif data == "drop":
        context.user_data["proposal"] = None
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Verworfen, nichts gespeichert.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    state = context.user_data.get("state")
    if state == "await_instruction":
        selected: EmailSummary = context.user_data["selected"]
        await update.effective_message.reply_text("Erstelle Entwurf …")
        instruction = None if text in ("", "-") else text
        context.user_data["instruction"] = instruction
        proposal = await _assistant(context).propose(selected.ref, instruction)
        await _send_proposal(update, context, proposal)
    elif state == "await_change" and context.user_data.get("proposal"):
        previous: DraftProposal = context.user_data["proposal"]
        await update.effective_message.reply_text("Überarbeite Entwurf …")
        proposal = await _assistant(context).propose(
            previous.ref, context.user_data.get("instruction"), previous=previous, change_request=text
        )
        await _send_proposal(update, context, proposal)
    else:
        await update.effective_message.reply_text(HELP)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, (MailError, LLMError)):
        message = f"Fehler: {error}"
    else:
        log.error("unhandled error: %s", type(error).__name__)
        message = "Unerwarteter Fehler. Details stehen im Server-Log."
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(message)


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


async def _gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler; drops updates from anyone not allowlisted."""
    user = update.effective_user if isinstance(update, Update) else None
    if user is None or user.id not in context.bot_data["allowed_user_ids"]:
        log.warning("ignored update from user id=%s", user.id if user else None)
        raise ApplicationHandlerStop


def run_bot(settings: Settings) -> None:
    app = build_application(settings)
    log.info("bot started, allowed users=%d, model=%s", len(settings.telegram_allowed_user_ids), settings.model)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
