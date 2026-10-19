# Konzept: Microsoft-Graph-Connector (Outlook / Microsoft 365)

Status: **Planung, nicht implementiert.** Geprüft gegen die Microsoft-Learn-Dokumentation am 19.10.2026 (Quellen unten). Punkte mit „offen“ sind noch nicht gegen die Doku geprüft und müssen vor der Umsetzung verifiziert werden.

## Ziel

Ein zweiter Connector neben `BaryonMail`, der denselben Vertrag `MailBackend` (`src/mailassist/mail.py`) erfüllt. Prozesslogik, Freigabe-Tor, Analyse, Wissensbasis und Prüfprotokoll bleiben unverändert.

## Abbildung des Vertrags

| `MailBackend` | Graph-Endpunkt | Status |
|---|---|---|
| `search(folder, query, limit)` | `GET /me/mailFolders/{id}/messages` mit `$top`, `$select`, Suche/Filter | offen: Such-Syntax (`$search` vs. `$filter`) und Kombinierbarkeit prüfen |
| `get_email(ref)` | `GET /me/messages/{id}` mit `$select` (u. a. `subject`, `from`, `replyTo`, `toRecipients`, `body`, `internetMessageId`, `conversationId`) | Eigenschaften belegt; offen: Header für Text- statt HTML-Body |
| `get_thread(ref)` | Nachrichten mit gleicher `conversationId` | Eigenschaft belegt; offen: Filter auf `conversationId` und Sortierung |
| `save_reply_draft(ref, body)` | `POST /me/messages/{id}/createReply` mit `message.body` | belegt: legt **nur einen Entwurf** an; `comment` und `message.body` schließen sich aus (sonst HTTP 400) |

**Antwortadresse:** Laut Doku soll bei gesetztem `replyTo` an diese Adressen statt an `from` geantwortet werden. Ob `createReply` das selbst übernimmt, ist offen. Gegebenenfalls `toRecipients` im `message`-Parameter aus `replyTo` setzen – analog zu Baryon, das die Empfänger aus Reply-To/From ableitet.

## Berechtigungen und Sicherheitsgrenze

| Berechtigung | Wofür | Entscheidung |
|---|---|---|
| `Mail.ReadWrite` (delegiert) | `createReply` verlangt sie als minimale Berechtigung (belegt) | **anfordern** |
| `Mail.Send` | `POST /messages/{id}/send` verlangt sie (belegt) | **nie anfordern** |
| Anwendungsberechtigungen | Zugriff auf *alle* Postfächer ohne angemeldeten Nutzer (belegt) | **nie verwenden** |

- **Stärker als bei Proton:** Ohne `Mail.Send` kann das Token einen Entwurf technisch nicht senden. Ob auch die Direkt-Endpunkte `reply`, `replyAll`, `forward` und `sendMail` an `Mail.Send` gebunden sind, ist offen (für `send` belegt).
- **Schwächer als bei Proton:** `Mail.ReadWrite` erlaubt laut Microsoft „create, read, update, and delete“. Es gibt keine Berechtigung nur für Entwürfe (belegt). Deshalb ist eine **Endpunkt-Allowlist im Code Pflicht**, analog zu `ALLOWED_TOOLS`:
  - erlaubt: `GET …/messages…`, `GET …/mailFolders…`, `POST …/messages/{id}/createReply`
  - verboten (im Client abgewiesen, bevor eine Anfrage rausgeht): `DELETE`, `PATCH`, `…/move`, `…/copy`, `…/send`, `…/reply`, `…/replyAll`, `…/forward`, `…/sendMail`, `…/permanentDelete`
- Admin-Zustimmung je nach Tenant-Richtlinie – mit der IT des Unternehmens klären.

## Identität, Verlauf, Duplikate

- `MessageRef` ist heute IMAP-spezifisch (`folder`, `uid`, `uidvalidity` als Zahlen). Graph-IDs sind Zeichenketten und ändern sich beim Verschieben, außer mit `Prefer: IdType="ImmutableId"` (belegt). **Umbau nötig:** `MessageRef` connector-neutral machen (z. B. `connector`, `opaque_id`, `folder`), für Graph unveränderliche IDs anfordern.
- `internetMessageId` (belegt) speist `message_key` im Prüfprotokoll und im Entwurfs-Register – Duplikat-Erkennung funktioniert damit connector-übergreifend gleich.

## Anmeldung

- Delegierter Login über MSAL mit Gerätecode-Verfahren (passt zu Server und Telegram-Betrieb). Neue Abhängigkeit `msal`, ggf. `msal-extensions` für einen verschlüsselten Token-Cache – Entscheidung bei der Umsetzung.
- Refresh-Token mit Dateirechten 600 außerhalb des Repos, nie in `.env` oder Logs; Schwärzung über `Settings.secrets()` erweitern.

## Abnahme (bei Umsetzung)

1. Dieselben Vertragstests wie für Baryon laufen gegen einen Fake-Graph (`httpx.MockTransport`) – inkl. Fallen-Endpunkten (`/send`, `DELETE`), die nie aufgerufen werden dürfen.
2. Test: Jeder verbotene Endpunkt wird im Client abgewiesen, ohne dass eine Anfrage rausgeht.
3. Live-Test mit einem Test-Postfach (nicht produktiv) und synthetischer Mail: lesen, Verlauf, Entwurf anlegen, Entwurf erscheint in Outlook, nichts gesendet.
4. Token-Scopes werden beim Start geprüft; enthält das Token `Mail.Send`, startet der Connector nicht.

## Quellen

- [message: createReply (v1.0)](https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0)
- [message: send (v1.0)](https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0)
- [message resource type (v1.0)](https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0)
- [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
