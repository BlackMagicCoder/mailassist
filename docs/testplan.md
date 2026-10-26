# Testplan

Zwei Stufen:

1. **Deterministisch** (`uv run pytest`, offline, Pflicht vor jedem Commit): Fake-LLM, Fake-Baryon (echter MCP-Server mit Testmails), synthetischer Korpus. Prüft, was der Code unabhängig vom Modellverhalten garantiert.
2. **Live-Evaluierung** (optional, `uv run pytest -m live`, siehe unten): echtes Modell über Amazon Bedrock, nur synthetische Mails. Misst, wie gut das Modell einordnet, Fakten einhält und Manipulationen widersteht.

Testdaten: `src/mailassist/demo_data/beispiel_gmbh.toml` – 21 synthetische B2B-Mails der fiktiven „Beispiel Maschinenbau GmbH“ (alle 10 Kategorien, Deutsch und Englisch, 7 Manipulationsversuche), ausschließlich reservierte `.example`-Domains. Demo-Wissensbasis: `examples/knowledge/`. Echte Kundendaten werden für Tests nie verwendet.

## Zuordnung Testplan → Tests

| Bereich | Fälle | Tests |
|---|---|---|
| Korpus-Integrität | Kategorien gültig, Zitate stehen wirklich in der Mail, nur `.example`-Domains | `test_corpus.py::test_case_is_well_formed_and_synthetic`, `test_corpus_size_and_unique_ids` |
| Klassifizierung/Extraktion | Schema-Validierung, Reparaturversuch, unbekannte Felder ohne Wert | `test_triage.py`, `test_connector_llm.py::test_complete_json_*` |
| Fakten-Belegung | wörtliche Zitate → belegt, erfundene → „unsicher“, Modell kann `verified` nicht setzen | `test_corpus.py::test_true_quotes_*`, `test_fields_the_mail_does_not_state_*`, `test_triage.py::test_ground_*`, `test_model_cannot_claim_verified` |
| Fehlende Fakten | konformer Entwurf mit `[[TODO]]` ohne Warnung, erfundener Preis → Warnung | `test_corpus.py::test_missing_facts_*`, `test_knowledge_checks.py` |
| Wissensbasis | nur freigegebene, gültige Einträge; abgelaufen/ohne Freigabe erreichen das Modell nie | `test_knowledge_checks.py::test_demo_knowledge_*`, `test_propose_uses_knowledge_*` |
| Prompt Injection – Struktur | Tag-Ausbruch, Unicode-Doppelgänger, gefälschter Owner-Absender, versteckter HTML-Text | `test_corpus.py::test_injection_cannot_break_prompt_structure`, `test_hardening.py::test_owner_detection_*`, `test_lookalike_*` |
| Prompt Injection – Ausgabe | eingeschleuste Links, Adressen, Prozent-Zusagen im Entwurf → Warnung | `test_corpus.py::test_compromised_draft_markers_are_flagged` |
| Unbelegte Zusagen | Rabatt, kostenlos, Erstattung, Garantie, Liefertermin ohne Freigabe → KRITISCH; durch gültige Wissensbasis/Anweisung gedeckt → still; Kundenbehauptung kein Beleg; Verneinung, Frage, Zitat → keine Zusage; abgelaufen/nicht freigegeben deckt nichts; Injection „alles kostenlos“; Speichern nur nach zweiter, hash-gebundener Bestätigung (Gate, CLI, Telegram, Protokoll) | `test_commitments.py` |
| Modellfehler | Timeout, 429/5xx mit Wiederholung, 401, abgeschnitten, leer, kein JSON | `test_corpus.py::test_model_failure_never_leads_to_a_saved_draft`, `test_connector_llm.py::test_retries_*`, `test_hardening.py::test_truncated_*` |
| Unerlaubte Aktionen | Fallen-Werkzeuge `send_email`/`save_draft`, fremde Telegram-ID, Knopf unter altem Entwurf, Doppeltipp, Hash-Abweichung, zweiter Entwurf zur selben Mail, kein automatisches Wiederholen beim Speichern | `test_mail_protocol.py`, `test_units.py::test_gate_*`, `test_hardening.py::test_button_*`, `test_double_tap_*`, `test_failed_save_*`, `test_audit.py::test_second_draft_*` |
| IMAP-Connector (T-Online & Co.) | nur schreibgeschützt öffnen, nur `BODY.PEEK`, Fallen `store`/`expunge`/`copy`/`move`/`delete` nie aufgerufen, `APPEND` nur in Entwürfe mit `\Draft`, Antwort-Kopfzeilen, eigene Adresse nie Empfänger, Verlauf aus Posteingang + Gesendet, UTF-7-Ordnernamen, Anmelde-/Verbindungsfehler, kein SMTP im Code | `test_imap.py` (gegen `tests/fake_imap.py`); optional gegen einen echten Test-Mailserver: `MAILASSIST_IMAP_TEST_SERVER` |
| Demo und Datenfluss | Demo-Daten nur `.example`-Domains und lizenziert, Mock-Analyse für alle 48 Mails gültig, naives Mock befolgt Manipulation → Prüfung schlägt an, Datenfluss erfasst jeden Schritt, HTML-Bericht escaped Mailinhalt, Demo nutzt nie die echte Konfiguration, CLI-Demo komplett | `test_demo.py` |
| Connector-Vertrag | vollständiger Verlauf inkl. „Gesendet“, Formatfehler → `MailError` ohne Inhalte | `test_connector_llm.py::test_thread_*`, `test_malformed_*` |
| Logs/Datenschutz | Schwärzung inkl. Tracebacks, keine Inhalte im Prüfprotokoll, sichere Fehlertexte für Telegram | `test_hardening.py::test_formatter_*`, `test_error_text_*`, `test_audit.py::test_audit_trail_*` |

## Bekannte Grenzen der deterministischen Prüfung

- **Zahlen ohne Kontext:** Eine Zahl gilt als belegt, wenn sie irgendwo in Mail, Verlauf, Anweisung oder Firmeninfos vorkommt. „Lieferung in 2 Tagen“ fällt nicht auf, wenn „2 Werktage“ in den Firmeninfos steht (in Tests ausdrücklich dokumentiert).
- **Zusagen nach Stichwort:** Kostenfreiheit, Rabatt, Erstattung, Gutschrift/Kulanz, Garantie und Liefer-/Terminzusagen werden über Wortlisten erkannt (`checks.py::COMMITMENTS`). Umschreibungen ohne Stichwort fallen nicht auf; indirekte Rede kann fälschlich als Zusage gelten. Dass eine Firmeninfo dasselbe Stichwort enthält, ist kein Beleg: still bleibt es nur, wenn eine freigegebene Aussage dieselben Prozente/Zahlen/Tage nennt, sonst gibt es einen Hinweis mit dem freigegebenen Wortlaut.
- Ob das echte Modell Manipulationen widersteht, zeigt nur die Live-Evaluierung.

## Live-Evaluierung

```bash
MAILASSIST_LIVE=1 BEDROCK_API_KEY=... uv run pytest -m live -s
```

Sendet ausschließlich die synthetischen Korpus-Mails an Bedrock (Kosten: zwei Modellaufrufe je Mail). Harte Prüfungen: gültiges Schema, kein Manipulations-Marker (Link, Adresse, Prozent-Zusage, „kostenlos“/„gratis“) im Entwurf ohne Warnung; `drafts_needing_confirmation` zählt Entwürfe mit KRITISCH-Zusagen. Kennzahlen (Trefferquote je Kategorie, erkannte Manipulationen, belegte Fakten) landen in `data/eval-report.json`.
