"""Command line: mailassist [--demo [--naiv]] [--datenfluss] neu | suche TEXT | analysiere NR | antworte NR | check | bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .approval import DuplicateDraftError
from .config import ConfigError, Settings
from .core import Assistant, DraftProposal
from .dataflow import DataFlow
from .errors import AssistantError
from .health import format_checks, run_checks
from .logs import setup_logging
from .models import EmailSummary, MessageRef
from .triage import format_triage

PRIVACY_NOTE = "Hinweis: Der Inhalt dieser Unterhaltung wird zur Entwurfserstellung an Amazon Bedrock (Claude) übertragen."
MOCK_NOTE = (
    "MOCK: echtes Postfach, aber lokales Regel-Modell ohne KI – Mailinhalte gehen an kein Sprachmodell. "
    "Gespeicherte Entwürfe landen wirklich im Entwürfe-Ordner (nur Platzhaltertext)."
)
DEMO_NOTE = (
    "DEMO: synthetische Mails (Beispiel GmbH + Gretel, Apache 2.0), lokales Mock-Modell ohne KI – "
    "nichts verlässt diesen Rechner, kein echtes Postfach."
)
RESULTS_FILE = "letzte_liste.json"


def _results_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / RESULTS_FILE


def _store_results(settings: Settings, results: list[EmailSummary]) -> None:
    # Only message references are stored locally, no subjects or content.
    path = _results_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([vars(r.ref) for r in results]), encoding="utf-8")


def _load_ref(settings: Settings, number: int) -> MessageRef:
    try:
        refs = json.loads(_results_path(settings).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("Keine Liste vorhanden. Zuerst 'mailassist neu' oder 'mailassist suche TEXT' ausführen.")
    if not 1 <= number <= len(refs):
        raise SystemExit(f"Nummer {number} gibt es in der letzten Liste nicht (1–{len(refs)}).")
    return MessageRef(**refs[number - 1])


def _print_results(results: list[EmailSummary]) -> None:
    if not results:
        print("Keine Nachrichten gefunden.")
    for i, r in enumerate(results, 1):
        print(f"{i:>3}. {r.date[:16].replace('T', ' ')}  {r.sender}\n     {r.subject}")


def _print_proposal(p: DraftProposal) -> None:
    print(f"\n--- Entwurf an {p.recipient} — {p.reply_subject} ({p.model}) ---\n")
    print(p.body)
    print("\n--- Ende des Entwurfs ---")
    if p.todo_count:
        print(f"Achtung: {p.todo_count} offene [[TODO]]-Stelle(n) vor dem Versand ergänzen.")
    for warning in p.warnings:
        print(f"Achtung: {warning}")


async def _reply(assistant: Assistant, ref: MessageRef, instruction: str | None, mailbox: str = "Postfach") -> None:
    proposal = await assistant.propose(ref, instruction)
    while True:
        _print_proposal(proposal)
        choice = input(f"\nAls Entwurf im {mailbox} speichern? [j]a / [n]ein / [ä]ndern: ").strip().lower()
        if choice in ("j", "ja", "y"):
            try:
                saved = await assistant.save(proposal)
            except DuplicateDraftError as exc:
                print(f"Achtung: {exc}")
                if input("Trotzdem speichern? [j/n]: ").strip().lower() not in ("j", "ja", "y"):
                    print("Nicht gespeichert.")
                    return
                saved = await assistant.save(proposal, force=True)
            print(f"Entwurf gespeichert: „{saved.subject}“ an {', '.join(saved.to)}. "
                  f"Bitte im {mailbox} prüfen und selbst senden.")
            return
        if choice in ("n", "nein"):
            print("Verworfen, nichts gespeichert.")
            return
        if choice in ("ä", "a", "aendern", "ändern"):
            change = input("Was soll geändert werden? ").strip()
            proposal = await assistant.propose(ref, instruction, previous=proposal, change_request=change)
            continue
        print("Bitte j, n oder ä eingeben.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailassist", description="KI-Antwortentwürfe für dein Postfach (sendet nie selbst).")
    parser.add_argument("--demo", action="store_true",
                        help="Demo: synthetisches Postfach und kostenloses Mock-Modell, kein Zugang und kein Schlüssel nötig")
    parser.add_argument("--mock", action="store_true",
                        help="echtes Postfach, aber lokales Mock-Modell statt Bedrock: kein Schlüssel, Mailinhalte gehen an kein Sprachmodell")
    parser.add_argument("--naiv", action="store_true",
                        help="nur mit --demo/--mock: Mock-Modell befolgt absichtlich Anweisungen aus Mails (zeigt die Schutzprüfungen)")
    parser.add_argument("--datenfluss", action="store_true",
                        help="bei analysiere/antworte zeigen, was gelesen, bereinigt, ans Modell gesendet und protokolliert wird")
    sub = parser.add_subparsers(dest="command", required=True)
    neu = sub.add_parser("neu", help="neueste Nachrichten im Posteingang auflisten")
    neu.add_argument("-n", type=int, default=10, help="Anzahl (Standard 10)")
    suche = sub.add_parser("suche", help="Postfach durchsuchen")
    suche.add_argument("text", nargs="+")
    suche.add_argument("-n", type=int, default=10, help="Anzahl (Standard 10)")
    analysiere = sub.add_parser("analysiere", help="Nachricht aus der letzten Liste einordnen und Fakten extrahieren")
    analysiere.add_argument("nummer", type=int)
    antworte = sub.add_parser("antworte", help="Antwortentwurf zu einer Nachricht aus der letzten Liste erstellen")
    antworte.add_argument("nummer", type=int)
    antworte.add_argument("--hinweis", help='z. B. "freundliche Absage"')
    antworte.add_argument("--sprache", help="Antwortsprache erzwingen, z. B. de oder en")
    sub.add_parser("bot", help="Telegram-Bot starten")
    check = sub.add_parser("check", help="Erreichbarkeit von Postfach, Bedrock und Telegram prüfen")
    check.add_argument("--llm", action="store_true", help="auch Bedrock prüfen (ein kleiner Modellaufruf)")
    check.add_argument("--telegram", action="store_true", help="auch den Telegram-Bot-Token prüfen")
    return parser


async def _telegram_probe(token: str) -> str:
    from telegram import Bot

    async with Bot(token) as telegram_bot:
        return "@" + (await telegram_bot.get_me()).username


async def _check(args: argparse.Namespace, settings: Settings, assistant: Assistant) -> None:
    probe = None
    if args.telegram:
        if not settings.telegram_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN ist nicht gesetzt.")
        probe = lambda: _telegram_probe(settings.telegram_token)  # noqa: E731
    results = await run_checks(settings, assistant.mail_session, assistant.llm if args.llm else None, probe)
    print(format_checks(results))
    if not all(r.ok for r in results):
        raise SystemExit(1)


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    assistant = Assistant.from_settings(settings)
    demo = settings.connector == "demo"
    if args.command == "check":
        await _check(args, settings, assistant)
        return
    if args.command == "neu":
        results = await assistant.recent(args.n)
    elif args.command == "suche":
        results = await assistant.search(" ".join(args.text), args.n)
    else:
        if not (demo or settings.use_mock):
            print(PRIVACY_NOTE)
        flow = DataFlow() if args.datenfluss else None
        assistant.tracer = flow
        try:
            if args.command == "analysiere":
                triage, model = await assistant.analyse(_load_ref(settings, args.nummer))
                print()
                print(format_triage(triage))
                print(f"(Modell: {model})")
            else:
                instruction = args.hinweis
                if args.sprache:
                    instruction = f"{instruction or 'Write an appropriate reply.'} Reply in language: {args.sprache}."
                await _reply(assistant, _load_ref(settings, args.nummer), instruction,
                             "Demo-Postfach" if demo else "Postfach")
        finally:
            if flow is not None and flow.steps:
                print(flow.summary())
                report = flow.write_html(Path(settings.data_dir) / "datenfluss", f"{args.command} Nr. {args.nummer}")
                print(f"HTML-Bericht mit dem vollständigen Wortlaut: {report.resolve()}")
        return
    _store_results(settings, results)
    _print_results(results)


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    load_dotenv()
    args = _parser().parse_args(argv)
    if args.naiv and not (args.demo or args.mock):
        raise SystemExit("--naiv gibt es nur zusammen mit --demo oder --mock.")
    if args.demo and args.mock:
        raise SystemExit("--demo nutzt das Mock-Modell bereits; --mock ist für das echte Postfach gedacht.")
    try:
        if args.demo:
            settings = Settings.for_demo(naive=args.naiv)
        else:
            settings = Settings.from_env(require_key=not args.mock)
            if args.mock:
                settings = settings.with_mock(naive=args.naiv)
    except ConfigError as exc:
        raise SystemExit(f"Konfigurationsfehler: {exc}")
    if args.command != "bot":
        if args.demo:
            print(DEMO_NOTE)
        elif args.mock:
            print(MOCK_NOTE)
    setup_logging(settings.secrets(), logging.INFO if args.command == "bot" else logging.WARNING)
    if args.command == "bot":
        from .telegram_bot import run_bot

        run_bot(settings)
        return
    try:
        asyncio.run(_run(args, settings))
    except AssistantError as exc:
        # The local terminal may show `detail` (server/Baryon text); Telegram never does.
        details = f"\nDetails: {exc.detail}" if exc.detail else ""
        raise SystemExit(f"Fehler: {exc}{details}")
    except FileNotFoundError:
        raise SystemExit(f"'{settings.baryon_command}' nicht gefunden. Ist Baryon MCP installiert und im PATH?")
    except KeyboardInterrupt:
        raise SystemExit("Abgebrochen, nichts gespeichert.")
