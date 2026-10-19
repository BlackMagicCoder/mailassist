# mailassist – KI-Antwortentwürfe für Proton Mail

mailassist liest eine von dir ausgewählte E-Mail samt Verlauf, lässt Claude Sonnet (über NanoGPT) einen Antwortentwurf schreiben und speichert ihn – nach deiner Bestätigung – als Entwurf in Proton Mail. **Versenden tust immer du selbst.** Bedienung per Kommandozeile oder Telegram.

```
Du (Telegram oder Terminal)
      │
  mailassist ──► Baryon MCP ──► Proton Mail Bridge (nur 127.0.0.1) ──► Proton
      │
      └──────► NanoGPT ──► Claude Sonnet   (erhält nur die gewählte Unterhaltung, keine Werkzeuge)
```

## Sicherheitsgrenzen

- **Kein Versand möglich.** Baryon hat kein Sende-Werkzeug; mailassist ruft zusätzlich nur vier freigegebene Werkzeuge auf (`search_emails`, `get_email`, `get_thread`, `save_reply_draft`) und verweigert alle anderen im Code. Das Modell bekommt überhaupt keine Werkzeuge – es kann nur Text zurückgeben.
- **Keine Änderungen an bestehenden Mails.** Nichts wird gelöscht, verschoben oder als gelesen markiert. Neu angelegt werden nur Entwürfe.
- **Speichern nur auf Bestätigung.** Ein Entwurf landet erst in Proton Mail, wenn du „j“ bzw. „Speichern“ wählst.
- **Mailinhalt gilt als nicht vertrauenswürdig.** Anweisungen in eingehenden Mails („ignoriere alle Regeln …“) werden dem Modell als Daten markiert und nicht befolgt.
- **Keine erfundenen Fakten.** Fehlt dem Modell eine Information (Termin, Preis, Zusage), setzt es `[[TODO: …]]`. Diese Stellen vor dem Versand ergänzen.
- **Telegram:** Der Bot bedient nur die Nutzer-IDs in `TELEGRAM_ALLOWED_USER_IDS`; alles andere wird verworfen.

## Datenschutz – bitte lesen

- **Der Inhalt der gewählten Unterhaltung wird an NanoGPT übertragen und von dort an Anthropic (Claude) weitergegeben.** Übertragen werden nur: die ausgewählte Mail, die bisherigen Nachrichten dieser Unterhaltung (gekürzt, max. ca. 40 000 Zeichen) und deine Anweisung. Andere Mails verlassen den Server nicht. Eine Seite zu Speicherdauer/Datenverwendung haben wir in der NanoGPT-Dokumentation nicht gefunden – prüfe die Bedingungen von NanoGPT selbst, bevor du vertrauliche Mails verarbeitest.
- **Telegram-Bot-Chats sind nicht Ende-zu-Ende-verschlüsselt.** Im Bot-Modus liegen Absender, Betreffzeilen und Entwurfstexte auch bei Telegram.
- **Gemieteter Server:** Bridge entschlüsselt dein Postfach auf dem Server. Wer den Server kontrolliert (auch der Hoster), kann technisch darauf zugreifen.
- **Logs** enthalten nur Nachrichten-IDs, Zeichenzahlen und den Modellnamen – keine Mailinhalte, keine Schlüssel (Schlüssel werden zusätzlich automatisch geschwärzt).
- Lokal gespeichert wird nur die letzte Trefferliste als Nachrichten-Referenzen (`data/letzte_liste.json`, ohne Betreff oder Inhalt).

## Voraussetzungen

- Proton-Konto mit Zugang zu **Proton Mail Bridge** (Bridge ist nach unserem Kenntnisstand kostenpflichtigen Tarifen vorbehalten – bitte prüfen)
- **Baryon MCP** v0.6.3 (<https://github.com/combor/baryon-mcp>)
- **uv** (<https://docs.astral.sh/uv/>), Python 3.12 wird von uv bereitgestellt
- **NanoGPT-API-Schlüssel** (<https://nano-gpt.com>)
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
cp .env.example .env && chmod 600 .env && nano .env    # NANOGPT_API_KEY eintragen
uv run mailassist neu                                  # Test: listet die neuesten Mails
```

**5. Telegram-Bot.**

1. In Telegram **@BotFather** → `/newbot` → Token in `.env` als `TELEGRAM_BOT_TOKEN` eintragen.
2. Eigene Nutzer-ID herausfinden: `TELEGRAM_ALLOWED_USER_IDS=0` setzen, `uv run mailassist bot` starten, dem Bot eine Nachricht schicken. Im Log erscheint `ignored update from user id=123456789`. Diese Zahl eintragen, Bot neu starten.
3. Als Dienst:

```bash
cp deploy/mailassist-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now mailassist-bot
journalctl --user -u mailassist-bot -f      # Log ansehen
```

## Lokale Installation (Windows, zum Ausprobieren)

1. Proton Mail Bridge für Windows installieren, anmelden, unter *Einstellungen → Erweitert → TLS-Zertifikate exportieren* `cert.pem` speichern.
2. Baryon: `baryon-mcp_0.6.3_windows_amd64.zip` und `baryon-mcp_0.6.3_SHA256SUMS` von der Release-Seite laden, `Get-FileHash baryon-mcp_*.zip -Algorithm SHA256` mit der Datei vergleichen, `baryon-mcp.exe` in einen Ordner im `PATH` entpacken, dann `baryon-mcp setup --skip-client-config`. Das Passwort landet in der Windows-Anmeldeinformationsverwaltung.
3. Im Projektordner: `uv sync`, `.env.example` nach `.env` kopieren, `NANOGPT_API_KEY` eintragen.

## Nutzung

### Telegram

| Du | Bot |
|---|---|
| `/neu` | neueste Mails im Posteingang als Buttons |
| `/suche Angebot Müller` | Suchtreffer im ganzen Postfach als Buttons |
| Button antippen | „Was soll die Antwort sagen?“ |
| `freundliche Absage` (oder `-` für einen allgemeinen Vorschlag) | zeigt den Entwurf mit **Speichern / Ändern / Verwerfen** |
| **Ändern** → `kürzer und förmlicher` | überarbeiteter Entwurf |
| **Speichern** | „Entwurf gespeichert … bitte in Proton Mail prüfen und selbst senden“ |

Danach in der Proton-Mail-App den Entwurf öffnen, `[[TODO]]`-Stellen ergänzen, senden.

### Kommandozeile

```bash
uv run mailassist neu                     # neueste Mails im Posteingang
uv run mailassist suche Angebot Müller    # Postfach durchsuchen
uv run mailassist antworte 2 --hinweis "freundliche Absage"
uv run mailassist antworte 2 --sprache en # Antwortsprache erzwingen
```

`antworte` zeigt den Entwurf und fragt: `[j]a` speichern, `[n]ein` verwerfen, `[ä]ndern` mit Änderungswunsch.

Die Antwort wird in der Sprache der eingegangenen Mail geschrieben, außer du gibst etwas anderes an. Mit `MAILASSIST_SIGNATURE` in `.env` bekommt jeder Entwurf deine Signatur.

## Fehlerbehebung

| Meldung / Symptom | Ursache und Lösung |
|---|---|
| `'baryon-mcp' nicht gefunden` | Baryon nicht installiert oder nicht im `PATH`. Alternativ vollen Pfad in `BARYON_COMMAND` setzen. |
| `Fehler: … connection refused` / Timeout beim Lesen | Bridge läuft nicht: `systemctl --user status protonmail-bridge` bzw. Bridge-App starten. |
| Fehler zu Zertifikat / TLS | Bridge-Zertifikat hat sich geändert: `baryon-mcp setup` erneut ausführen. |
| Fehler zu Zugangsdaten | Bridge-Passwort geändert: `baryon-mcp setup --reset-credentials`. |
| `NanoGPT antwortete mit HTTP 401` | API-Schlüssel falsch oder abgelaufen (`.env` prüfen). |
| `HTTP 402` / `429` | Guthaben aufgebraucht bzw. zu viele Anfragen – im NanoGPT-Konto prüfen. |
| `Konfigurationsfehler: …` | Pflichtwert in `.env` fehlt, Meldung nennt ihn. |
| Bot reagiert nicht | Eigene ID nicht in `TELEGRAM_ALLOWED_USER_IDS` (Log zeigt `ignored update from user id=…`), oder Dienst gestoppt: `journalctl --user -u mailassist-bot`. |
| Keine Treffer bei `/suche` | Suche läuft im Ordner `All Mail`. Heißt er bei dir anders, `MAILASSIST_SEARCH_FOLDER` setzen. |
| Entwurf erscheint nicht in der Handy-App | Bridge synchronisiert Entwürfe zu Proton; kurz warten bzw. App aktualisieren. |

## Entwicklung

```bash
uv run pytest        # alle Tests, ohne Netz und ohne echtes Postfach
```

Die Tests nutzen Mock-Daten: `tests/fake_baryon.py` ist ein echter MCP-Server mit Testmails, der zusätzlich die Fallen `send_email` und `save_draft` anbietet – die Tests prüfen, dass mailassist sie nie aufruft. NanoGPT wird per `httpx.MockTransport` simuliert.

### Stand der Prüfung

- **Automatisch getestet:** MCP-Kommunikation, Werkzeug-Allowlist, Prompt-Aufbau inkl. Schutz gegen eingeschleuste Tags, NanoGPT-Anfrageformat, Schlüssel-Schwärzung in Logs, CLI- und Bot-Ablauf (Auswählen → Entwurf → Ändern → Speichern/Verwerfen), Zugriffssperre für fremde Telegram-Nutzer.
- **Noch nicht getestet** (braucht Bridge, Schlüssel und Token): echte Verbindung zu Proton Mail Bridge, echte Antwort von NanoGPT, echter Telegram-Betrieb, Server-Installation. Ersten Live-Test bitte mit einer unkritischen Testmail machen.
