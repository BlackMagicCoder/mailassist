"""Build src/mailassist/demo_data/gretel_de.json from the Gretel dataset (Apache 2.0).

Reproducible, run once by hand (pyarrow is only needed here, not by mailassist):

    curl -LO https://huggingface.co/datasets/gretelai/synthetic_pii_finance_multilingual/resolve/main/data/German_train-00000-of-00001.parquet
    uv run --no-project --with pyarrow python scripts/build_demo_data.py German_train-00000-of-00001.parquet

Selection: German rows of type "Email" (with a Subject/Betreff line) and
"Customer support conversational log" (only logs that split cleanly into
customer / support turns), quality_score >= 85, 300-2500 characters, in dataset
order. Modifications (documented in demo_data/NOTICE.md): email domains and URL
hosts that are not already reserved example domains are rewritten to *.example.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pyarrow.parquet as pq

OUT = Path(__file__).parents[1] / "src" / "mailassist" / "demo_data" / "gretel_de.json"
MAX_EMAILS, MAX_LOGS = 26, 10
CUSTOMER = re.compile(r"^\s*\*\*(Customer|Kunde|Kundin)( \([^)]*\))?:?\*\*:?\s*$", re.IGNORECASE)
AGENT = re.compile(r"^\s*\*\*(Support Agent|Agent|Support|Kundendienst|Mitarbeiter)( \([^)]*\))?:?\*\*:?\s*$", re.IGNORECASE)
OWNER = "Kundenservice Demo <service@gretel-demo.example>"


def _reserved(host: str) -> bool:
    return host.endswith(".example") or host in {"example.com", "example.org", "example.net"} or host.endswith(
        (".example.com", ".example.org", ".example.net"))


def neutralise_domains(text: str) -> str:
    def host(h: str) -> str:
        h = h.lower().rstrip(".")
        return h if _reserved(h) else re.sub(r"[^a-z0-9-]", "-", h) + ".example"

    text = re.sub(r"(?<=@)([\w-]+(?:\.[\w-]+)+)", lambda m: host(m.group(1)), text)
    return re.sub(r"(?<=://)([\w-]+(?:\.[\w-]+)+)", lambda m: host(m.group(1)), text)


def parse_email(text: str) -> dict | None:
    lines = text.strip().splitlines()
    headers, body_start = {}, 0
    for i, line in enumerate(lines[:6]):
        m = re.match(r"^\**\s*(Subject|Betreff|Von|An|From|To)\s*\**\s*:\s*\**\s*(.+?)\s*\**\s*$", line, re.IGNORECASE)
        if m:
            headers[m.group(1).lower()] = m.group(2)
            body_start = i + 1
        elif headers and line.strip():
            break
    subject = headers.get("subject") or headers.get("betreff")
    if not subject:
        return None
    sender = headers.get("von") or headers.get("from")
    return {"subject": subject, "from": sender, "body": "\n".join(lines[body_start:]).strip()}


def parse_log(text: str) -> list[dict] | None:
    turns, current = [], None
    for line in text.splitlines():
        if CUSTOMER.match(line):
            current = {"own": False, "lines": []}
            turns.append(current)
        elif AGENT.match(line):
            current = {"own": True, "lines": []}
            turns.append(current)
        elif line.strip() == "---" or re.match(r"^\s*\*\*(Timestamp|Interaction Type|Time)", line):
            current = None
        elif current is not None:
            current["lines"].append(line)
    turns = [{"own": t["own"], "body": "\n".join(t["lines"]).strip()} for t in turns if "\n".join(t["lines"]).strip()]
    while turns and turns[-1]["own"]:  # the demo needs an unanswered customer message at the end
        turns.pop()
    if len(turns) < 2 or not any(t["own"] for t in turns):
        return None
    return turns


def main(parquet: str) -> None:
    rows = pq.read_table(parquet).to_pylist()
    mails, n_mail, n_log = [], 0, 0
    for row in rows:
        text, quality = row["generated_text"], row["quality_score"]
        if row["language"] != "German" or quality < 85 or not 300 <= len(text) <= 2500:
            continue
        ident = f"gretel-{row['__index_level_0__']}"
        if row["document_type"] == "Email" and n_mail < MAX_EMAILS:
            parsed = parse_email(text)
            if parsed and len(parsed["body"]) > 120:
                n_mail += 1
                sender = parsed["from"] or f"Absender {n_mail} <absender-{n_mail}@gretel-demo.example>"
                if "@" not in sender:
                    sender = f"{sender} <absender-{n_mail}@gretel-demo.example>"
                mails.append({"id": ident, "source": "gretel", "gretel_index": row["__index_level_0__"], "document_type": "Email",
                              "from": neutralise_domains(sender), "subject": neutralise_domains(parsed["subject"]),
                              "body": neutralise_domains(parsed["body"]), "thread": []})
        elif row["document_type"] == "Customer support conversational log" and n_log < MAX_LOGS:
            turns = parse_log(text)
            if turns:
                n_log += 1
                customer = f"Kunde {n_log} <kunde-{n_log}@gretel-demo.example>"
                *earlier, last = turns
                mails.append({"id": ident, "source": "gretel", "gretel_index": row["__index_level_0__"],
                              "document_type": "Customer support conversational log", "from": customer,
                              "subject": f"Ihre Anfrage an den Kundenservice ({n_log})",
                              "body": neutralise_domains(last["body"]),
                              "thread": [{"from": OWNER if t["own"] else customer, "own": t["own"],
                                          "body": neutralise_domains(t["body"])} for t in earlier]})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(mails, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{n_mail} emails + {n_log} support logs -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
