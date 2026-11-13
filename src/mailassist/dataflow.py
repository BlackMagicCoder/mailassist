"""Data-flow view: records what happens to a mail on its way through mailassist and
renders it for the terminal and as a self-contained HTML report.

The recorder only receives what core.Assistant reports (see Assistant._trace): the
mail as read, what was defused, which approved facts were used, the exact text sent
to the model, its answer, the checks and the audit entries. Secrets never pass here.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

TITLES = {
    "gelesen": "Aus dem Postfach gelesen",
    "bereinigt": "Bereinigt, bevor etwas ans Modell geht",
    "wissensbasis": "Freigegebene Firmeninformationen",
    "an_modell": "An das Modell gesendet",
    "vom_modell": "Antwort des Modells",
    "belegpruefung": "Belegprüfung der Analyse",
    "pruefung": "Prüfung des Entwurfs vor der Freigabe",
    "protokoll": "Prüfprotokoll (lokal, ohne Inhalte)",
}
NOT_SENT = ("andere Mails im Postfach", "API-Schlüssel und Zugangsdaten", "das Prüfprotokoll",
            "versteckter HTML-Text")


class DataFlow:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, step: str, details: dict[str, Any]) -> None:
        self.steps.append((step, details))

    # --- terminal --------------------------------------------------------------

    def grouped(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Consecutive steps of the same kind (e.g. several audit entries) form one section."""
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        for step, details in self.steps:
            if groups and groups[-1][0] == step and step == "protokoll":
                groups[-1][1].append(details)
            else:
                groups.append((step, [details]))
        return groups

    def summary(self) -> str:
        lines = ["", "═══ Datenfluss ═══"]
        for n, (step, items) in enumerate(self.grouped(), 1):
            lines.append(f"{n}. {TITLES.get(step, step)}")
            lines.extend(f"   {line}" for d in items for line in self._describe(step, d))
        lines.append(f"Nie ans Modell gesendet: {', '.join(NOT_SENT)}.")
        return "\n".join(lines)

    @staticmethod
    def _describe(step: str, d: dict[str, Any]) -> list[str]:
        if step == "gelesen":
            return [f"Von: {d['absender']} · Betreff: {d['betreff']}",
                    f"{d['zeichen']} Zeichen{' (gekürzt)' if d['gekuerzt'] else ''}, Verlauf: {d['verlauf']} Nachricht(en)"]
        if step == "bereinigt":
            out = [f"Imitierte Steuer-Tags entschärft: {d['tag_imitationen']}",
                   f"Texte mit Unicode-Doppelgängern normalisiert: {d['unicode_normalisiert']}"]
            return out + list(d["hinweise"]) if d["hinweise"] else out + ["Kein versteckter HTML-Text gefunden"]
        if step == "wissensbasis":
            used = ", ".join(d["verwendet"]) or "keine passenden Einträge"
            return [f"Verwendet: {used}"] + [f"Nicht verwendet: {s}" for s in d["uebersprungen"]]
        if step == "an_modell":
            tags = sorted(set(re.findall(r"<(selected_email|thread|company_facts|owner_instruction|change_request|previous_draft)>", d["user"])))
            return [f"Anweisungen (System): {len(d['system'])} Zeichen · Inhalt: {len(d['user'])} Zeichen",
                    f"Bestandteile: {', '.join(tags)}",
                    "Vollständiger Wortlaut im HTML-Bericht."]
        if step == "vom_modell":
            return [f"Modell: {d['modell']} · {len(d['text'])} Zeichen"]
        if step == "belegpruefung":
            out = [f"{d['belegt']} von {d['fakten']} Angaben wörtlich in der Mail belegt"]
            if d["herabgestuft"]:
                out.append(f"Als „unsicher“ markiert (nicht belegt): {', '.join(map(str, d['herabgestuft']))}")
            if d["manipulationsverdacht"]:
                out.append("⚠️ Manipulationsverdacht gemeldet")
            return out
        if step == "pruefung":
            critical = [f"🛑 {c}" for c in d.get("kritisch", [])]
            binding = [f"Freigabe gilt nur für Entwurf {d['entwurf_id']} mit Inhalts-Hash {d['hash']}"] if "hash" in d else []
            return [f"Offene [[TODO]]-Stellen: {d['todos']}"] + critical + (
                d["warnungen"] or ([] if critical else ["Keine ungestützten Angaben gefunden"])) + binding
        if step == "protokoll":
            return [json.dumps(d, ensure_ascii=False)]
        return [json.dumps(d, ensure_ascii=False)]

    # --- HTML report -----------------------------------------------------------

    def _last(self, step: str) -> dict[str, Any] | None:
        return next((d for s, d in reversed(self.steps) if s == step), None)

    def flagged(self) -> list[str]:
        """Values the last draft check found unbacked - highlighted in the mail text as well."""
        check = self._last("pruefung") or {}
        return [b["wert"] for line in check.get("zeilen", []) for b in line["befunde"]]

    def html(self, title: str) -> str:
        sections = []
        for n, (step, group) in enumerate(self.grouped(), 1):
            d = group[-1]
            items = "".join(f"<li>{html.escape(line)}</li>" for g in group for line in self._describe(step, g))
            extra = ""
            if step == "gelesen" and "text" in d:
                extra = _mail_html(d["text"], self.flagged())
            elif step == "pruefung" and "zeilen" in d:
                extra = _draft_html(d)
            elif step == "an_modell":
                extra = (f"<details open><summary>Anweisungen an das Modell (System)</summary><pre>{_mark(d['system'])}</pre></details>"
                         f"<details open><summary>Inhalt an das Modell</summary><pre>{_mark(d['user'])}</pre></details>")
            elif step == "vom_modell":
                extra = f"<details open><summary>Antwort</summary><pre>{html.escape(d['text'])}</pre></details>"
            sections.append(f"<section><h2>{n}. {html.escape(TITLES.get(step, step))}</h2><ul>{items}</ul>{extra}</section>")
        not_sent = "".join(f"<li>{html.escape(x)}</li>" for x in NOT_SENT)
        return _PAGE.format(title=html.escape(title), body="".join(sections), not_sent=not_sent,
                            created=datetime.now().strftime("%d.%m.%Y %H:%M"))

    def write_html(self, directory: str | Path, title: str) -> Path:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"datenfluss-{datetime.now():%Y%m%d-%H%M%S}.html"
        path.write_text(self.html(title), encoding="utf-8")
        return path


_TAG_IMITATION = re.compile(r"&lt;/?(?:selected_email|thread|company_facts|owner_instruction|change_request|previous_draft)\b.*?&gt;")
_FINDING_LABEL = {"nur_mail": "nur aus der Mail", "ohne_quelle": "ohne Quelle", "zahl": "nicht belegt"}


def _highlight(escaped: str, values: list[str], css: str, link: bool = False) -> str:
    for value in sorted(set(values), key=len, reverse=True):
        v = html.escape(value)
        mark = f'<mark class="{css}">{v}</mark>'
        escaped = escaped.replace(v, f'<a href="#mail-text">{mark}</a>' if link else mark)
    return escaped


def _mail_html(text: str, flagged: list[str]) -> str:
    """The mail as read: tag imitations (defused before the model sees them) and values the
    draft check found unbacked are marked where they stand."""
    marked = _TAG_IMITATION.sub(
        lambda m: f'<mark title="Tag-Imitation, wird vor dem Modell entschärft">{m.group(0)}</mark>', html.escape(text))
    marked = _highlight(marked, [v for v in flagged if v in text], "bad")
    return f'<details open id="mail-text"><summary>Mail-Text</summary><pre>{marked}</pre></details>'


def _draft_html(d: dict[str, Any]) -> str:
    """The draft line by line with where its figures come from, plus what the approval binds to."""
    rows = []
    for line in d["zeilen"]:
        findings = line["befunde"]
        if findings or line["kritisch"]:
            css = "bad"
            label = " · ".join(dict.fromkeys(_FINDING_LABEL[b["art"]] for b in findings)) or "Zusage ohne Freigabe"
        elif {"Firmeninfo", "Anweisung"} & set(line["quellen"]):
            css, label = "ok", " · ".join(line["quellen"])
        else:
            css, label = "plain", " · ".join(line["quellen"]) or "Text"
        text = _highlight(html.escape(line["text"]), [b["wert"] for b in findings], "bad",
                          link=any(b["art"] == "nur_mail" for b in findings))
        rows.append(f'<div class="ln {css}"><span>{text}</span><span class="src">{html.escape(label)}</span></div>')
    if d["kritisch"]:
        status = ("bad", f"{len(d['kritisch'])} Zusage(n) ohne Freigabe – Speichern erst nach ausdrücklicher Bestätigung")
    elif d["warnungen"]:
        status = ("warn", f"{len(d['warnungen'])} Hinweis(e) – bitte vor der Freigabe lesen")
    else:
        status = ("ok", "Keine ungestützten Angaben")
    gate = (f'<div class="gate"><span class="status {status[0]}">{html.escape(status[1])}</span>'
            f'<span>Die Freigabe gilt nur für Entwurf <code>{html.escape(d["entwurf_id"])}</code> mit Inhalts-Hash '
            f'<code>{html.escape(d["hash"])}</code>. Jede Änderung am Text ergibt einen neuen Vorschlag mit neuem Hash; '
            "die alte Freigabe gilt dann nicht mehr.</span></div>")
    legend = ('<p class="meta"><span class="ok-t">Grün</span> = aus deiner Anweisung oder den freigegebenen Firmeninfos · '
              '<span class="bad-t">Rot</span> = keine vertrauenswürdige Quelle (Klick führt zur Stelle in der Mail) · '
              "ohne Farbe = Text oder Angaben aus Mail und Signatur</p>")
    return (f'<h3>Entwurf mit Herkunft <small>({html.escape(d.get("modell", ""))})</small></h3>{legend}'
            f'<div class="draft">{"".join(rows)}</div>{gate}')


def _mark(text: str) -> str:
    """Escape, then highlight our structure tags and the places where mail text was defused (‹…)."""
    escaped = html.escape(text)
    escaped = re.sub(r"&lt;/?[a-z_]+(?: author=&quot;\w+&quot;)?&gt;", lambda m: f'<span class="tag">{m.group(0)}</span>', escaped)
    return re.sub(r"‹/?[a-z_]+", lambda m: f'<mark title="entschärft: aus der Mail, wirkt nicht als Steuer-Tag">{m.group(0)}</mark>', escaped)


_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Datenfluss – mailassist</title>
<style>
:root {{ --bg:#fafafa; --fg:#18181b; --muted:#52525b; --card:#fff; --line:#e4e4e7; --tag:#2563eb; --mark:#fde68a;
  --ok:#15803d; --ok-bg:#dcfce7; --warn:#a16207; --warn-bg:#fef3c7; --bad:#b91c1c; --bad-bg:#fee2e2; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#18181b; --fg:#f4f4f5; --muted:#a1a1aa; --card:#27272a; --line:#3f3f46; --tag:#93c5fd; --mark:#a16207;
  --ok:#86efac; --ok-bg:#14361f; --warn:#fcd34d; --warn-bg:#3d2f0a; --bad:#fca5a5; --bad-bg:#451a1a; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:960px; margin:auto; }}
h1 {{ font-size:22px; margin:0 0 4px; }} p.meta {{ color:var(--muted); margin:0 0 20px; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 16px; margin:0 0 12px; }}
h2 {{ font-size:16px; margin:4px 0 8px; }}
pre {{ white-space:pre-wrap; word-break:break-word; background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:10px; font-size:13px; }}
.tag {{ color:var(--tag); font-weight:600; }} mark {{ background:var(--mark); color:inherit; }}
summary {{ cursor:pointer; color:var(--muted); }}
h3 {{ font-size:14px; margin:14px 0 4px; }} h3 small {{ color:var(--muted); font-weight:400; }}
mark.bad {{ background:var(--bad-bg); color:var(--bad); font-weight:600; }}
.ok-t {{ color:var(--ok); font-weight:600; }} .bad-t {{ color:var(--bad); font-weight:600; }}
.draft {{ border:1px solid var(--line); border-radius:6px; padding:6px; display:grid; gap:2px; font-size:14px; }}
.ln {{ display:grid; grid-template-columns:1fr auto; gap:10px; padding:3px 8px; border-radius:4px; white-space:pre-wrap; overflow-wrap:anywhere; }}
.ln .src {{ font-size:11px; color:var(--muted); white-space:nowrap; padding-top:2px; }}
.ln.ok {{ background:var(--ok-bg); }} .ln.ok .src {{ color:var(--ok); }}
.ln.bad {{ background:var(--bad-bg); }} .ln.bad .src {{ color:var(--bad); font-weight:600; }}
.gate {{ display:grid; gap:6px; margin-top:10px; font-size:13px; color:var(--muted); }}
.status {{ justify-self:start; font-weight:600; padding:4px 8px; border-radius:6px; }}
.status.ok {{ color:var(--ok); background:var(--ok-bg); }} .status.warn {{ color:var(--warn); background:var(--warn-bg); }}
.status.bad {{ color:var(--bad); background:var(--bad-bg); }}
</style></head><body><main>
<h1>Datenfluss: {title}</h1>
<p class="meta">Erstellt {created}. <span class="tag">Blau</span> = Steuer-Tags von mailassist; <mark>gelb</mark> = aus der Mail stammende Tag-Imitation, entschärft.</p>
{body}
<section><h2>Nie ans Modell gesendet</h2><ul>{not_sent}</ul></section>
</main></body></html>
"""
