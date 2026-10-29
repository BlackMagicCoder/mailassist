# mailassist – Inbox-to-Action Copilot für Ihr Postfach

**Deutsch** · [English](README.en.md)

## Wofür

Für Vertrieb und Kundenservice in kleinen und mittleren Unternehmen, die täglich ähnliche Anfragen beantworten: Angebote, Liefertermine, Reklamationen, Rückfragen. mailassist nimmt die Routinearbeit ab, ohne die Kontrolle abzugeben:

- **Einordnen statt lesen:** Kategorie, Dringlichkeit und die wichtigen Angaben der Mail (Menge, Artikel, Termin …) – jede Angabe mit der Textstelle, aus der sie stammt.
- **Antwortentwurf aus Ihren eigenen Vorgaben:** Preise, Lieferzeiten und Konditionen kommen nur aus freigegebenen Firmeninfos. Fehlt etwas, steht dort `[[TODO: …]]` statt einer erfundenen Zahl.
- **Der Mensch entscheidet:** Der Entwurf landet erst nach Freigabe im Entwürfe-Ordner. Senden kann mailassist technisch nicht.
- **Schutz vor Fehlern:** Sagt ein Entwurf etwas zu, das niemand freigegeben hat – kostenlos, Rabatt, Erstattung, Garantie, Liefertermin –, lässt er sich nur nach einer zweiten, ausdrücklichen Bestätigung speichern. Mails, die das Modell zu solchen Zusagen überreden wollen, werden markiert.
- **Daten in der EU:** Das Sprachmodell läuft über Amazon Bedrock in Frankfurt; nur die gewählte Unterhaltung wird übertragen.

Nachweise: 210 automatische Tests (offline, nur synthetische Daten), zwei Live-Auswertungen mit 21 Test-Mails inkl. 7 Manipulationsversuchen und ein Abnahmetest mit einem echten GMX-Postfach (Details unter [Grenzen](#grenzen--bitte-kennen) und [Stand der Prüfung](#stand-der-prüfung)).

Anbindung an das eigene Postfach und die eigenen Firmeninfos als Auftrag: [kbouschen.com](https://www.kbouschen.com)

## So funktioniert es

mailassist ordnet eine ausgewählte Geschäftsmail ein, extrahiert die relevanten Angaben strukturiert, schlägt nächste Schritte vor und schreibt einen Antwortentwurf auf Basis **freigegebener Unternehmensinformationen**. Der Entwurf wird erst nach deiner ausdrücklichen Freigabe im Entwürfe-Ordner deines Postfachs gespeichert. **Versenden tust immer du selbst.** Bedienung per Kommandozeile oder Telegram.

Postfächer: **Proton Mail** (bezahlter Tarif, über Proton Mail Bridge), **T-Online und andere IMAP-Postfächer** (auch kostenlose) – oder die **Demo** ganz ohne Postfach.

```
Du (Telegram oder Terminal)
      │  Auswahl, Anweisung, Freigabe
  mailassist ──► Baryon MCP ──► Proton Mail Bridge (nur 127.0.0.1) ──► Proton
      │
      ├── Analyse: Kategorie, Fakten mit Belegstelle, offene Fragen, nächste Schritte (nur Vorschläge)
      ├── Entwurf: nur aus Mail, Verlauf, deiner Anweisung und der freigegebenen Wissensbasis
      ├── Prüfung: ungestützte Zahlen, Links, Adressen, Prozent-Zusagen → Warnung vor der Freigabe
      └──────► Amazon Bedrock (Frankfurt) ──► gpt-oss-120b   (erhält nur die gewählte Unterhaltung, keine Werkzeuge)
```

## Architektur

| Schicht | Modul | Aufgabe |
|---|---|---|
| E-Mail-Connector | `mail.py`, `imap.py`, `demo.py` | Proton über Baryon MCP (Werkzeug-Allowlist) · beliebiges IMAP-Postfach wie T-Online (Befehls-Wächter: nur lesen, Entwürfe ablegen) · Demo-Postfach mit synthetischen Mails |
| Prozesslogik | `core.py`, `triage.py`, `knowledge.py`, `checks.py`, `prompt.py` | Lesen → Einordnen → Entwurf → Prüfen; Mailinhalt gilt als nicht vertrauenswürdig |
| LLM-Adapter | `llm.py` | Amazon Bedrock Converse-API (Standard: OpenAI gpt-oss-120b in Frankfurt, Bedrock-API-Schlüssel), Wiederholung bei Störungen, JSON-Modus mit Validierung |
| Freigabe | `approval.py`, `audit.py` | Freigabe an Entwurf + Inhalts-Hash gebunden, Duplikat-Erkennung, Prüfprotokoll ohne Inhalte |
| Oberflächen | `cli.py`, `telegram_bot.py`, `dataflow.py` | dünne Schichten über dem Kern; Datenfluss-Ansicht |

Ein späterer Microsoft-Graph-Connector ist geplant, aber nicht umgesetzt: [docs/graph-connector.md](docs/graph-connector.md).

## Demo – ohne Postfach, ohne Schlüssel, ohne Kosten

```bash
uv sync
uv run mailassist --demo neu                                # synthetisches Postfach
uv run mailassist --demo --datenfluss analysiere 2          # Einordnung + was mit den Daten passiert
uv run mailassist --demo --datenfluss antworte 8 --hinweis "Angebot zusagen"
uv run mailassist --demo --naiv --datenfluss antworte 8     # Modell, das Manipulationen befolgt – Prüfung schlägt an
uv run mailassist --demo check --llm
uv run mailassist --demo bot                                # Telegram-Demo (braucht nur TELEGRAM_BOT_TOKEN + Nutzer-ID)
```

- **Daten:** 48 synthetische Mails – 21 eigene B2B-Mails der fiktiven „Beispiel Maschinenbau GmbH“ (inkl. 7 Manipulationsversuchen) und 27 Mails aus dem Gretel-Datensatz *synthetic_pii_finance_multilingual* (Apache 2.0, künstliche Personendaten). Herkunft, Lizenz und Änderungen: [`src/mailassist/demo_data/NOTICE.md`](src/mailassist/demo_data/NOTICE.md).
- **Mock-Modell:** regelbasiert, lokal, kostenlos – **keine KI**. Es liest aber genau den Text, den ein echtes Modell bekäme. `--naiv` lässt es absichtlich Anweisungen aus Mails befolgen (Links, Prozent-Rabatte, „kostenlos“), damit man sieht, wie die Prüfung vor der Freigabe anschlägt.
- **Datenfluss** (`--datenfluss`): zeigt Schritt für Schritt, was gelesen, was entfernt/entschärft (versteckter HTML-Text, imitierte Steuer-Tags), welche freigegebenen Firmeninfos verwendet, was ans Modell geschickt und was protokolliert wurde – im Terminal und als HTML-Bericht unter `data/demo/datenfluss/` mit dem vollständigen Wortlaut.
- Alles bleibt lokal unter `data/demo/` (Entwürfe in `demo_entwuerfe.json`); die echte Konfiguration und echte Daten werden nie benutzt.

## Sicherheitsgrenzen

- **Kein Versand möglich.** Baryon hat kein Sende-Werkzeug (Quellcode v0.6.3 geprüft); mailassist ruft zusätzlich nur vier freigegebene Werkzeuge auf (`search_emails`, `get_email`, `get_thread`, `save_reply_draft`) und verweigert alle anderen im Code. Das Modell bekommt keine Werkzeuge – es kann nur Text zurückgeben.
- **Bei IMAP (T-Online & Co.)** greift der Connector nur über einen Wächter auf die Verbindung zu: Ordner werden schreibgeschützt geöffnet, Mails ohne „gelesen“-Markierung abgerufen, abgelegt wird nur in den Entwürfe-Ordner mit Entwurfs-Kennzeichen. Löschen, Verschieben, Flags setzen gibt es dort nicht – und SMTP (Senden) kommt in mailassist überhaupt nicht vor (per Test geprüft).
- **Keine Änderungen an bestehenden Mails.** Nichts wird gelöscht, verschoben oder als gelesen markiert. Neu angelegt werden nur Entwürfe.
- **Freigabe an den Entwurf gebunden.** Gespeichert wird nur der Entwurf, dessen ID und Inhalts-Hash freigegeben wurden – einmal, von dir. Ein Knopf unter einem älteren Entwurf speichert nichts. Ein fehlgeschlagenes Speichern wird nie automatisch wiederholt (Bridge könnte den Entwurf trotzdem angelegt haben).
- **Kein zweiter Entwurf ohne Rückfrage.** Wurde zu einer Mail schon ein Entwurf gespeichert, fragt mailassist nach.
- **Mailinhalt gilt als nicht vertrauenswürdig.** Anweisungen in eingehenden Mails werden als Daten markiert und nicht befolgt; Abgrenzungs-Tags lassen sich auch mit Doppelgänger-Zeichen nicht imitieren; versteckter HTML-Text erreicht das Modell nicht; gefälschte Absendernamen gelten nicht als „du“.
- **Keine erfundenen Fakten.** Preise, Fristen, Konditionen und andere Zusagen nur aus der Wissensbasis oder deiner Anweisung, sonst `[[TODO: …]]`.
- **Unbelegte Zusagen brauchen eine zweite Bestätigung.** Sagt der Entwurf etwas zu – kostenlos, Rabatt, Erstattung, Gutschrift/Kulanz, Garantie, Liefertermin –, das weder deine Anweisung noch eine freigegebene Firmeninfo deckt, wird er als KRITISCH markiert. Speichern geht dann nur nach einer eigenen Bestätigung dieses Entwurfs (Kommandozeile: `zusage` eintippen; Telegram: „Zusage bestätigen und speichern“). Was die Kundenmail behauptet („wie zugesagt …“), gilt nie als Beleg.
- **Nächste Schritte sind Vorschläge.** Nichts davon wird ausgeführt; Schritte, die ein anderes System ändern würden, sind gekennzeichnet.
- **Baryon-Version festgelegt.** Startet eine andere als die geprüfte Version, verweigert mailassist den Zugriff (`MAILASSIST_BARYON_VERSION`).
- **Telegram:** Der Bot bedient nur die Nutzer-IDs in `TELEGRAM_ALLOWED_USER_IDS`; alles andere wird vor jeder Verarbeitung verworfen.

## Datenschutz – bitte lesen

- **Der Inhalt der gewählten Unterhaltung wird an Amazon Bedrock (AWS) übertragen und dort vom Modell gpt-oss-120b verarbeitet** – für die Analyse und für jeden Entwurf. Standard ist die Region Frankfurt (`eu-central-1`); die Anfragen verlassen diese Region nicht. Übertragen werden: die ausgewählte Mail, die bisherigen Nachrichten dieser Unterhaltung (gekürzt, max. ca. 40 000 Zeichen), deine Anweisung und passende Einträge der Wissensbasis. Andere Mails verlassen den Server nicht. Laut [AWS-Dokumentation](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html) haben Modellanbieter (hier OpenAI) keinen Zugriff auf Prompts und Antworten; prüfe die AWS-Bedingungen selbst, bevor du vertrauliche Mails verarbeitest.
- **Telegram-Bot-Chats sind nicht Ende-zu-Ende-verschlüsselt.** Im Bot-Modus liegen Absender, Betreffzeilen, Analyse und Entwurfstexte auch bei Telegram. Fehlermeldungen an Telegram enthalten nie Server- oder Mailtexte.
- **Gemieteter Server:** Bridge entschlüsselt dein Postfach auf dem Server. Wer den Server kontrolliert (auch der Hoster), kann technisch darauf zugreifen.
- **Logs** enthalten nur IDs, Zählwerte und den Modellnamen – keine Mailinhalte. Schlüssel werden zusätzlich automatisch geschwärzt, auch in Tracebacks.
- **Lokal gespeichert** (alles unter `data/`, von git ignoriert, unter Linux mit Rechten 600):
  - `letzte_liste.json` – Nachrichten-Referenzen der letzten Trefferliste, ohne Betreff oder Inhalt
  - `audit.jsonl` – Prüfprotokoll: Zeitpunkt, Ereignis, Entwurfs-ID, gekürzter Hash der Message-ID und des Entwurfs, Modell, Grund – **kein Inhalt** (per Test abgesichert)
  - `mailassist.sqlite` – für welche Nachrichten (Hash) schon ein Entwurf gespeichert wurde

## Mit T-Online (oder einem anderen IMAP-Postfach)

Für kostenlose Postfächer ohne Proton Bridge. Kein Baryon nötig.

1. **Am besten eine eigene, neue Adresse zum Testen anlegen** – nicht das Hauptpostfach.
2. Im Telekom E-Mail-Center unter den Einstellungen bei **„POP3- / SMTP- / IMAP-Zugang“** ein **Passwort für E-Mail-Programme** festlegen. Das normale Telekom-Login-Passwort wird nicht verwendet und gehört nicht in die `.env`.
3. In der `.env`:
   ```
   MAILASSIST_CONNECTOR=imap
   IMAP_USER=deine-adresse@t-online.de
   IMAP_PASSWORD=dein-passwort-fuer-e-mail-programme
   # IMAP_HOST=secureimap.t-online.de   (Standard, Port 993, verschlüsselt)
   ```
4. `uv run mailassist --mock check` – zeigt, ob die Anmeldung klappt und welche Ordner für **Entwürfe** und **Gesendet** erkannt wurden. Wird kein Entwürfe-Ordner gefunden, den Namen aus der Meldung mit `IMAP_DRAFTS_FOLDER` setzen.
5. **Erst kostenlos mit `--mock` testen:** `uv run mailassist --mock neu`, `--mock analysiere 1`, `--mock antworte 1`. Das lokale Regel-Modell braucht keinen Bedrock-Schlüssel, Mailinhalte gehen an kein Sprachmodell – aber das Postfach ist echt: ein gespeicherter (Platzhalter-)Entwurf landet wirklich im Entwürfe-Ordner.
6. Danach ohne `--mock` mit dem echten Modell (braucht `BEDROCK_API_KEY`). Entwürfe landen im Entwürfe-Ordner; versenden tust du selbst im Telekom-Webmailer oder Mailprogramm.

Unterschiede zu Proton: Es gibt keinen Ordner „All Mail“ – gesucht wird im Posteingang (`MAILASSIST_SEARCH_FOLDER` ändert das), der Verlauf wird über die Antwort-Kopfzeilen aus Posteingang und „Gesendet“ zusammengesetzt. Andere Anbieter (GMX, web.de, Posteo, mailbox.org, Gmail mit App-Passwort …) funktionieren über `IMAP_HOST`; bei manchen muss IMAP erst in den Einstellungen aktiviert werden.

> **Stand:** Der IMAP-Connector ist gegen einen nachgebauten IMAP-Client getestet (inkl. T-Online-typischer Ordnernamen wie „Entwürfe“) **und gegen einen echten lokalen Test-Mailserver** (GreenMail 2.1.14 in Docker): Lesen, Verlauf aus Posteingang + Gesendet, Entwurf mit Antwort-Kopfzeilen im Entwürfe-Ordner; eine unabhängige Prüfung auf dem Server bestätigte, dass keine Mail als gelesen markiert und nichts gelöscht wurde. Gegen einen echten Anbieter ist er seit dem 29.10.2026 abgenommen: **GMX-Testpostfach** (`imap.gmx.net`) mit `--mock` und mit dem echten Modell – Ordner „Entwürfe“/„Gesendet“ automatisch erkannt, KI-Entwurf als Antwort verknüpft im Entwürfe-Ordner, Doppel-Entwurf-Rückfrage und KRITISCH-Sperre greifen, eine Manipulations-Testmail wurde erkannt und vom Modell ignoriert. Eine unabhängige, schreibgeschützte Prüfung auf dem Server bestätigte: nichts gesendet („Gesendet“ leer), nichts gelöscht oder verschoben, die nur von mailassist gelesene Mail blieb ungelesen. Gegen T-Online selbst lief er noch nicht.
>
> Test-Mailserver selbst starten: `docker run -d --name mailassist-greenmail -p 127.0.0.1:3143:3143 -e "GREENMAIL_OPTS=-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=kira.test:test123@t-online.example" greenmail/standalone:2.1.14`, dann `MAILASSIST_IMAP_TEST_SERVER=127.0.0.1:3143:kira.test:test123 uv run pytest -m imap_server`.

## Voraussetzungen (Proton-Variante)

- Proton-Konto mit Zugang zu **Proton Mail Bridge** (Bridge ist nach unserem Kenntnisstand kostenpflichtigen Tarifen vorbehalten – bitte prüfen)
- **Baryon MCP** v0.6.3 (<https://github.com/combor/baryon-mcp>)
- **uv** (<https://docs.astral.sh/uv/>), Python 3.12 wird von uv bereitgestellt
- **Amazon-Bedrock-API-Schlüssel** (AWS-Konsole → Amazon Bedrock → *API keys*); `uv run mailassist check --llm` zeigt, ob das Modell aufrufbar ist
- für Telegram: ein Bot-Token von **@BotFather**

## Installation auf dem Server (Linux-VPS, empfohlen: Debian 12 / Ubuntu 24.04)

> Diese Schritte folgen der Bridge- und Baryon-Dokumentation bzw. verbreiteten Headless-Anleitungen. Sie wurden **noch nicht auf einem echten Server durchgespielt** – Abweichungen bitte melden, dann wird die Anleitung korrigiert.

**1. Eigener Benutzer, kein root.** SSH nur mit Schlüssel. Es muss **kein** Port außer SSH geöffnet werden – Bridge lauscht nur lokal, der Bot holt Nachrichten aktiv bei Telegram ab.

```bash
sudo adduser mailassist
sudo loginctl enable-linger mailassist     # User-Dienste laufen ohne Login weiter
sudo -iu mailassist
```

**2. Proton Mail Bridge (headless).** Aktuelles `.deb` von <https://proton.me/mail/bridge> laden und installieren. Ohne Desktop braucht Bridge `pass` als Passwortspeicher:

```bash
sudo apt install ./protonmail-bridge_*.deb pass gnupg
gpg --batch --passphrase '' --quick-gen-key 'ProtonMail Bridge' default default never
pass init "ProtonMail Bridge"
protonmail-bridge --cli
>>> login          # Proton-Zugangsdaten + 2FA
>>> info           # zeigt IMAP-Benutzername und Bridge-Passwort – notieren
>>> exit
```

Als Dienst starten:

```bash
mkdir -p ~/.config/systemd/user
cp ~/mailassist/deploy/protonmail-bridge.service ~/.config/systemd/user/   # nach Schritt 4
systemctl --user daemon-reload && systemctl --user enable --now protonmail-bridge
```

**3. Baryon MCP.** Paket und Prüfsummen von der Release-Seite laden, Prüfsumme vergleichen, installieren, einrichten:

```bash
V=0.6.3; B=https://github.com/combor/baryon-mcp/releases/download/v$V
curl -LO $B/baryon-mcp_${V}_linux_amd64.deb -LO $B/baryon-mcp_${V}_SHA256SUMS
sha256sum --ignore-missing -c baryon-mcp_${V}_SHA256SUMS
sudo apt install ./baryon-mcp_${V}_linux_amd64.deb
baryon-mcp setup --skip-client-config
```

`setup` fragt Bridge-Benutzername und -Passwort ab und bietet unter Linux an, das Zertifikat der laufenden Bridge zu übernehmen (Fingerabdruck wird angezeigt). Ohne Keyring legt Baryon das Passwort in einer nur für dich lesbaren Datei unter `~/.config/baryon-mcp` ab.

**4. mailassist.**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Projekt nach ~/mailassist kopieren (git clone oder scp/rsync)
cd ~/mailassist
uv sync --no-dev
cp .env.example .env && chmod 600 .env && nano .env    # BEDROCK_API_KEY eintragen
mkdir -p knowledge                                      # Wissensbasis anlegen (siehe unten)
uv run mailassist check --llm                           # Ampel: Konfiguration, Postfach, Bedrock
```

**5. Telegram-Bot.**

1. In Telegram **@BotFather** → `/newbot` → Token in `.env` als `TELEGRAM_BOT_TOKEN` eintragen.
2. Eigene Nutzer-ID herausfinden: `TELEGRAM_ALLOWED_USER_IDS=0` setzen, `uv run mailassist bot` starten, dem Bot eine Nachricht schicken. Im Log erscheint `ignored update from user id=123456789`. Diese Zahl eintragen, Bot neu starten.
3. Prüfen und als Dienst starten:

```bash
uv run mailassist check --telegram
cp deploy/mailassist-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now mailassist-bot
journalctl --user -u mailassist-bot -f      # Log ansehen
```

## Lokale Installation (Windows, zum Ausprobieren)

1. Proton Mail Bridge für Windows installieren, anmelden, unter *Einstellungen → Erweitert → TLS-Zertifikate exportieren* `cert.pem` speichern.
2. Baryon: `baryon-mcp_0.6.3_windows_amd64.zip` und `baryon-mcp_0.6.3_SHA256SUMS` von der Release-Seite laden, `Get-FileHash baryon-mcp_*.zip -Algorithm SHA256` mit der Datei vergleichen, `baryon-mcp.exe` in einen Ordner im `PATH` entpacken, dann `baryon-mcp setup --skip-client-config`. Das Passwort landet in der Windows-Anmeldeinformationsverwaltung.
3. Im Projektordner: `uv sync`, `.env.example` nach `.env` kopieren, `BEDROCK_API_KEY` eintragen, `uv run mailassist check`.

## Wissensbasis (freigegebene Unternehmensinformationen)

Eine Markdown-Datei pro Thema im Ordner `knowledge/` (anpassbar über `MAILASSIST_KNOWLEDGE_DIR`). Verwendet wird eine Datei nur mit Freigabe im Kopf:

```markdown
---
title: Lieferbedingungen
approved_by: Vertrieb (K. Muster)
approved_on: 2026-10-01
valid_until: 2026-12-31      # optional
---
- Lieferzeit Förderbänder: 3 bis 4 Wochen ab Auftragsbestätigung.
```

Ohne `approved_by`/`approved_on`, mit Freigabedatum in der Zukunft oder nach `valid_until` wird die Datei übersprungen (Grund im Log und in `mailassist check`). Änderungen wirken ab dem nächsten Entwurf, ohne Neustart. Ein vollständiges Beispiel einer fiktiven Firma liegt in `examples/knowledge/` (zum Ausprobieren: `MAILASSIST_KNOWLEDGE_DIR=examples/knowledge`).

## Nutzung

### Telegram

| Du | Bot |
|---|---|
| `/neu` | neueste Mails im Posteingang als Buttons |
| `/suche Angebot Müller` | Suchtreffer im ganzen Postfach als Buttons |
| Button antippen | **Analyse**: Kategorie, Dringlichkeit, Fakten (✓ belegt / unsicher / unbekannt), Fragen des Absenders, vorgeschlagene nächste Schritte – dann „Was soll die Antwort sagen?“ |
| `freundliche Absage` (oder `-` für einen allgemeinen Vorschlag) | zeigt den vollständigen Entwurf (lange Entwürfe in mehreren Teilen) mit Hinweisen und **Speichern / Ändern / Verwerfen** unter dem letzten Teil |
| **Ändern** → `kürzer und förmlicher` | überarbeiteter Entwurf; die Knöpfe des alten Entwurfs verschwinden |
| **Speichern** | „Entwurf gespeichert … bitte in Proton Mail prüfen und selbst senden“ |

Danach in der Proton-Mail-App den Entwurf öffnen, `[[TODO]]`-Stellen und markierte Angaben prüfen, senden.

### Kommandozeile

```bash
uv run mailassist neu                     # neueste Mails im Posteingang
uv run mailassist suche Angebot Müller    # Postfach durchsuchen
uv run mailassist analysiere 2            # einordnen, Fakten extrahieren, nächste Schritte vorschlagen
uv run mailassist antworte 2 --hinweis "freundliche Absage"
uv run mailassist antworte 2 --sprache en # Antwortsprache erzwingen
uv run mailassist check [--llm] [--telegram]   # Erreichbarkeit prüfen
```

`antworte` zeigt den Entwurf mit Hinweisen und fragt: `[j]a` speichern, `[n]ein` verwerfen, `[ä]ndern` mit Änderungswunsch. Steht unter dem Entwurf eine KRITISCH-Zeile, fragt mailassist nach `j` zusätzlich nach `zusage`; Enter speichert nichts.

Die Antwort wird in der Sprache der eingegangenen Mail geschrieben, außer du gibst etwas anderes an. Mit `MAILASSIST_SIGNATURE` in `.env` bekommt jeder Entwurf deine Signatur.

## Grenzen – bitte kennen

- **Die Ausgabeprüfung ist kontextfrei.** Sie findet Zahlen, Links, Adressen und Prozentangaben, die *nirgends* belegt sind. Eine belegte Zahl im falschen Zusammenhang („Lieferung in 2 Tagen“, weil „2 Werktage“ für Ersatzteile in der Wissensbasis steht) fällt nicht auf.
- **Die Zusagen-Prüfung arbeitet mit Stichwörtern, nicht mit Textverständnis.** Sie erkennt Kostenfreiheit, Rabatt, Erstattung, Gutschrift/Kulanz, Garantie und Liefer-/Terminzusagen über Wortlisten (Deutsch und etwas Englisch), überspringt Fragen, Zitate in Anführungszeichen und verneinte Satzteile und vergleicht Prozente, Zahlen und Tage mit deiner Anweisung und den Firmeninfos. Umschreibungen ohne Stichwort („das übernehmen wir für Sie“) erkennt sie nicht; indirekte Rede („Sie schreiben, die Lieferung sei kostenlos“) wird fälschlich als Zusage gemeldet. Passt das Stichwort zu einer Firmeninfo, aber nicht genau, gibt es nur einen Hinweis mit dem freigegebenen Wortlaut zum Abgleich – keinen Beleg.
- **Die Analyse ist eine Einschätzung des Modells.** Belegt (✓) heißt: das Zitat steht wörtlich in der Mail – nicht, dass die Deutung stimmt.
- Wie gut das echte Modell einordnet und Manipulationen widersteht, misst die Live-Evaluierung (siehe [docs/testplan.md](docs/testplan.md)). Stand 26.10.2026 mit gpt-oss-120b, 21 synthetische Mails: 76 % richtig eingeordnet, 5 von 7 Manipulationsversuchen erkannt, in 2 Entwürfen erschlichene Rabatte (30 %, 40 %) übernommen – beide vor der Freigabe als „nicht belegt“ markiert. Zweiter Lauf nach Einführung der Zusagen-Prüfung (gleicher Tag, Einzellauf, keine Statistik): 81 % richtig eingeordnet, 4 von 7 Manipulationen erkannt, 1 Entwurf mit erschlichenem Rabatt (30 %) und „Lieferung morgen“ – als KRITISCH markiert, Speichern nur nach Bestätigung; dazu 1 legitimer Entwurf mit nicht freigegebener Erstattungszusage (ebenfalls KRITISCH), 4 Entwürfe nur mit Hinweisen. **Warnungen also immer lesen.**

## Fehlerbehebung

Erster Schritt immer: `uv run mailassist check --llm --telegram`.

| Meldung / Symptom | Ursache und Lösung |
|---|---|
| `'baryon-mcp' nicht gefunden` | Baryon nicht installiert oder nicht im `PATH`. Alternativ vollen Pfad in `BARYON_COMMAND` setzen. |
| `baryon-mcp meldet Version …` | Installierte Baryon-Version weicht von der geprüften ab. Richtige Version installieren oder – nach eigener Prüfung – `MAILASSIST_BARYON_VERSION` anpassen. |
| `Postfach-Zugriff fehlgeschlagen` / Timeout | Bridge läuft nicht: `systemctl --user status protonmail-bridge` bzw. Bridge-App starten. In der Kommandozeile stehen unter „Details“ die Originalmeldungen. |
| Fehler zu Zertifikat / TLS | Bridge-Zertifikat hat sich geändert: `baryon-mcp setup` erneut ausführen. |
| Fehler zu Zugangsdaten | Bridge-Passwort geändert: `baryon-mcp setup --reset-credentials`. |
| `Bedrock antwortete mit HTTP 401/403` | API-Schlüssel falsch oder abgelaufen (`.env` prüfen; Bedrock-Schlüssel haben ein Ablaufdatum). Steht unter „Details“ *not available for this account*, ist das Modell für dein AWS-Konto nicht freigeschaltet – anderes Modell in `MAILASSIST_MODEL` oder Zugang bei AWS anfragen. |
| `Bedrock antwortete mit HTTP 400` / `404` | Modell-ID gibt es in dieser Region nicht (`MAILASSIST_MODEL` und `BEDROCK_REGION` prüfen; Profile mit `eu.`/`us.`-Präfix passen nur zur gleichnamigen Region). Für Claude-Modelle verlangt AWS zusätzlich ein Anthropic-Formular in der Bedrock-Konsole. |
| `HTTP 429` | Zu viele Anfragen (Kontingent im AWS-Konto). Bei 429 und 5xx versucht mailassist es automatisch bis zu zweimal erneut. |
| `Modellantwort wurde abgeschnitten` | `MAILASSIST_MAX_TOKENS` erhöhen oder die Anweisung kürzen. |
| `Speichern fehlgeschlagen … könnte trotzdem angelegt worden sein` | Im Entwürfe-Ordner nachsehen, bevor du erneut speicherst. |
| `Zu dieser Mail wurde bereits ein Entwurf gespeichert` | Absichtlich: erst nach Bestätigung wird ein weiterer gespeichert. |
| `Dieser Entwurf ist veraltet` | Du hast einen Knopf unter einem älteren Entwurf gedrückt – den aktuellen verwenden. |
| `Konfigurationsfehler: …` | Pflichtwert in `.env` fehlt, Meldung nennt ihn. |
| `IMAP-Anmeldung fehlgeschlagen` | Bei T-Online das **Passwort für E-Mail-Programme** verwenden (nicht das Telekom-Login-Passwort); prüfen, ob der IMAP-Zugang im E-Mail-Center aktiviert ist. |
| `IMAP-Server nicht erreichbar` | `IMAP_HOST`/`IMAP_PORT` prüfen (T-Online: `secureimap.t-online.de`, 993), Internetverbindung, Firewall. |
| `Entwürfe: NICHT GEFUNDEN` in `check` | Ordnernamen aus der Meldung übernehmen: `IMAP_DRAFTS_FOLDER=…` in `.env`. |
| Verlauf ohne eigene Antworten (IMAP) | „Gesendet“-Ordner nicht erkannt: `IMAP_SENT_FOLDER=…` setzen. |
| Bot reagiert nicht | Eigene ID nicht in `TELEGRAM_ALLOWED_USER_IDS` (Log zeigt `ignored update from user id=…`), oder Dienst gestoppt: `journalctl --user -u mailassist-bot`. |
| Keine Treffer bei `/suche` / unvollständiger Verlauf | Suche und Verlauf nutzen den Ordner `All Mail`. Heißt er bei dir anders, `MAILASSIST_SEARCH_FOLDER` setzen. |
| Wissensbasis wird nicht benutzt | `mailassist check` zeigt geladene und übersprungene Dateien samt Grund. |
| Entwurf erscheint nicht in der Handy-App | Bridge synchronisiert Entwürfe zu Proton; kurz warten bzw. App aktualisieren. |

## Entwicklung und Tests

```bash
uv run pytest                                  # alle Tests, offline, ohne echtes Postfach
MAILASSIST_LIVE=1 uv run pytest -m live -s     # optional: Live-Evaluierung mit synthetischen Mails
```

Testdaten sind ausschließlich synthetisch (`src/mailassist/demo_data/beispiel_gmbh.toml`, `examples/knowledge/`). `tests/fake_baryon.py` ist ein echter MCP-Server mit Ordnern wie bei Bridge und mit Fallen-Werkzeugen (`send_email`, `save_draft`), die nie aufgerufen werden dürfen. Zuordnung aller Testfälle: [docs/testplan.md](docs/testplan.md).

### Stand der Prüfung

- **Automatisch getestet (offline):** Werkzeug-Allowlist und MCP-Protokoll, vollständiger Verlauf über Ordner hinweg, validierte Baryon-Antworten, Freigabe-Bindung (veraltete Knöpfe, Doppeltipp, Hash, fremde Nutzer), Duplikat-Erkennung, Prüfprotokoll ohne Inhalte, Analyse mit Belegprüfung, Wissensbasis mit Freigabe/Ablauf, Ausgabeprüfung, 21 synthetische Mails inkl. 7 Manipulationsversuchen, Modellfehler (Timeout, 429/5xx, abgeschnitten, kein JSON), Schwärzung in Logs, CLI- und Bot-Abläufe.
- **Live getestet:** Amazon Bedrock (gpt-oss-120b, Frankfurt) mit den 21 synthetischen Mails (zwei Läufe, Ergebnisse unter [Grenzen](#grenzen--bitte-kennen)); IMAP-Connector mit echtem Modell gegen ein GMX-Testpostfach (siehe [Stand](#mit-t-online-oder-einem-anderen-imap-postfach)).
- **Noch nicht getestet:** echte Verbindung zu Proton Mail Bridge, T-Online-Server, echter Telegram-Betrieb, Server-Installation. Ersten Live-Test bitte mit einer unkritischen Testmail machen.
