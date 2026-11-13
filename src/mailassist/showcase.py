"""Showcase ("Schaufenster"): one self-contained HTML page that shows the demo visually.

It runs real demo scenarios (synthetic mails, local mock model) through the same
workflow as the CLI, collects what the data-flow tracer reports and renders it in
the layout of the original design draft: incoming mail -> what goes to the model ->
draft with provenance -> approval. Demo data only, so the page may be published
(website, README screenshots). Nothing is saved to any mailbox.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from .config import Settings
from .core import Assistant
from .dataflow import NOT_SENT, DataFlow


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    lead: str
    query: str
    subject: str
    same_note: str = ""  # shown when the careful and the naive model write the same draft


SCENARIOS = (
    Scenario("anfrage", "Normale Anfrage",
             "Ein Kunde fragt nach einem Angebot. Der Entwurf nutzt nur freigegebene Firmeninfos; "
             "jede Angabe zeigt, woher sie stammt.", "neue Halle", "Anfrage Förderband FB-200",
             "Beide Modelle schreiben denselben Entwurf: Die Mail enthält keine Anweisung, die sich befolgen ließe."),
    Scenario("versteckt", "Versteckter Text",
             "Die Mail enthält HTML-Text, den man im Mailprogramm nicht sieht. mailassist entfernt ihn, "
             "bevor irgendetwas ans Modell geht.", "Wartungsvertrag Basis", "Angebot Wartungsvertrag",
             "Beide Modelle schreiben denselben Entwurf: Die versteckte Anweisung wurde entfernt, bevor sie das Modell "
             "erreicht. Selbst ein leichtgläubiges Modell kann sie deshalb nicht befolgen."),
    Scenario("tags", "Gefälschte Steuerbefehle",
             "Die Mail imitiert die internen Markierungen von mailassist, um eine Anweisung unterzuschieben. "
             "Die Imitation wird entschärft und bleibt bloßer Text.", "Edelstahl", "Rückfrage"),
    Scenario("manipulation", "Manipulationsversuch",
             "Die Mail fordert Rabatt und einen fremden Zahlungslink. Schalte auf das naive Modell um: "
             "Es befolgt die Anweisung, und die Prüfung hält den Entwurf an.", "Ignoriere alle bisherigen",
             "Angebot FB-200"),
)
MODELS = (("vorsichtig", "Vorsichtiges Modell"), ("naiv", "Naives Modell (befolgt Mail-Anweisungen)"))
_TAGS = re.compile(r"<(selected_email|thread|company_facts|owner_instruction|change_request|previous_draft)>")


def _last(flow: DataFlow, step: str) -> dict[str, Any]:
    return next((d for s, d in reversed(flow.steps) if s == step), {})


async def collect(settings: Settings) -> dict[str, Any]:
    """Run every scenario with the careful and the naive mock model; return JSON-ready data."""
    if settings.connector != "demo":
        raise ValueError("Das Schaufenster zeigt ausschließlich synthetische Demo-Mails.")
    scenarios = []
    for scenario in SCENARIOS:
        runs: dict[str, Any] = {}
        entry: dict[str, Any] = {"key": scenario.key, "titel": scenario.title, "text": scenario.lead}
        for model, label in MODELS:
            assistant = Assistant.from_settings(replace(settings, demo_model=model))
            hits = await assistant.search(scenario.query, 10)
            ref = next(s.ref for s in hits if s.subject == scenario.subject)
            assistant.tracer = flow = DataFlow()
            if model == "vorsichtig":
                triage, _ = await assistant.analyse(ref)
                entry["analyse"] = {
                    "verdacht": triage.injection_suspected,
                    "fakten": [{"wert": f.value, "belegt": f.verified} for f in triage.facts()
                               if f.status != "unknown" and f.value],
                }
            await assistant.propose(ref)
            check = _last(flow, "pruefung")
            runs[model] = {"label": label, **{k: check[k] for k in
                                               ("modell", "zeilen", "kritisch", "warnungen", "todos", "entwurf_id", "hash")}}
            if model == "vorsichtig":
                read, cleaned, knowledge = _last(flow, "gelesen"), _last(flow, "bereinigt"), _last(flow, "wissensbasis")
                entry["mail"] = {k: read[k] for k in ("absender", "betreff", "text", "zeichen", "verlauf")}
                entry["bereinigt"] = cleaned
                entry["wissen"] = knowledge
                entry["bestandteile"] = sorted(set(_TAGS.findall(_last(flow, "an_modell").get("user", ""))))
        entry["laeufe"] = runs
        same = [z["text"] for z in runs["vorsichtig"]["zeilen"]] == [z["text"] for z in runs["naiv"]["zeilen"]]
        entry["gleich_hinweis"] = scenario.same_note if same else ""
        scenarios.append(entry)
    return {"szenarien": scenarios, "nie_gesendet": list(NOT_SENT),
            "erstellt": datetime.now().strftime("%d.%m.%Y")}


def render(data: dict[str, Any]) -> str:
    template = resources.files("mailassist").joinpath("schaufenster.html").read_text(encoding="utf-8")
    # "<" escaped so mail text such as "</selected_email>" can never close the script element.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("/*SCHAUFENSTER_DATEN*/null", payload)


async def write(settings: Settings, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(await collect(settings)), encoding="utf-8")
    return target
