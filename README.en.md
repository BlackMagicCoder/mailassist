# mailassist – an inbox-to-action copilot

[Deutsch](README.md) · **English**

## What it is for

For sales and customer-service teams in small and mid-sized companies who answer similar requests every day: quotes, delivery dates, complaints, follow-up questions. mailassist takes over the routine part without taking control away:

- **Triage instead of reading:** category, urgency and the key details of a mail (quantity, item, date …) – each detail with the passage it was taken from.
- **Reply drafts from your own approved information:** prices, lead times and terms come only from approved company facts. If something is missing, the draft says `[[TODO: …]]` instead of making a number up.
- **A human decides:** a draft reaches the drafts folder only after explicit approval. mailassist has no way to send mail.
- **Guard against mistakes:** if a draft promises something nobody approved – free of charge, a discount, a refund, a guarantee, a delivery date – it can only be saved after a second, explicit confirmation. Mails that try to talk the model into such promises are flagged.
- **Data stays in the EU:** the language model runs on Amazon Bedrock in Frankfurt; only the selected conversation is sent.

Evidence: 210 automated tests (offline, synthetic data only), two live evaluations on 21 test mails including 7 manipulation attempts, and an acceptance test against a real GMX mailbox (see [Results and limits](#results-and-limits)).

The user interface (command line and Telegram bot) and the detailed setup guide are in German, because the target users are German-speaking businesses. Connecting mailassist to your own mailbox and company information is available as a project: [kbouschen.com](https://www.kbouschen.com)

## How it works

```
You (Telegram or terminal)
      │  select a mail, give an instruction, approve
  mailassist ──► mailbox connector: IMAP (GMX, T-Online, …) or Proton Mail via Bridge + Baryon MCP
      │
      ├── triage: category, facts with evidence, open questions, next steps (suggestions only)
      ├── draft: built only from the mail, its thread, your instruction and the approved knowledge base
      ├── checks: unbacked numbers, links, addresses, percentages and business promises → warnings before approval
      └──────► Amazon Bedrock (Frankfurt) ──► gpt-oss-120b   (receives only the selected conversation, no tools)
```

| Layer | Modules | Role |
|---|---|---|
| Mail connectors | `mail.py`, `imap.py`, `demo.py` | Proton via Baryon MCP (tool allowlist) · any IMAP mailbox (command guard: read, store drafts, nothing else) · demo mailbox with synthetic mails |
| Process | `core.py`, `triage.py`, `knowledge.py`, `checks.py`, `prompt.py` | read → triage → draft → check; mail content is treated as untrusted |
| LLM adapter | `llm.py` | Amazon Bedrock Converse API, retries, JSON mode with schema validation |
| Approval | `approval.py`, `audit.py` | approval bound to proposal id + content hash, duplicate detection, content-free audit log |
| Front ends | `cli.py`, `telegram_bot.py`, `dataflow.py` | thin layers over the core; data-flow view |

## Safety boundaries

- **Sending is impossible.** There is no SMTP code in the project (enforced by a test). The IMAP connector only talks to the server through a guard that allows read-only folder access, fetching without setting the "seen" flag, and appending to the drafts folder. The Proton connector calls four allowlisted tools and refuses everything else. The model gets no tools – it can only return text.
- **Existing mail is never changed.** Nothing is deleted, moved, flagged or marked as read.
- **Approval is bound to the exact draft.** Only the draft whose id and content hash were approved is saved – once, by the owner. A button under an older draft saves nothing. A failed save is never retried automatically.
- **No second draft without asking.** If a draft was already saved for a mail, mailassist asks first.
- **Mail content is untrusted.** Instructions inside incoming mail are treated as data; delimiter tags cannot be forged, not even with look-alike Unicode characters; hidden HTML text never reaches the model; spoofed sender names are not treated as the owner.
- **No invented facts.** Prices, deadlines and terms only from the knowledge base or the owner's instruction, otherwise `[[TODO: …]]`. Every number, link, address and percentage in a draft must be backed by a source the owner can vouch for, or it is flagged.
- **Unbacked promises need a second confirmation.** Business commitments (free of charge, discount, refund, credit, guarantee, delivery date) are matched per clause. If neither the owner's instruction nor an approved fact covers them, the draft is marked CRITICAL and the approval gate refuses to save it until that exact draft is confirmed separately. A customer's claim ("as promised, delivery is free") is never evidence.
- **Next steps are suggestions.** Nothing is executed.

## Try the demo – no mailbox, no API key, no cost

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run mailassist --demo neu                                # synthetic inbox ("neu" = newest mails)
uv run mailassist --demo --datenfluss analysiere 2          # triage + what happens to the data
uv run mailassist --demo --datenfluss antworte 8 --hinweis "Angebot zusagen"
uv run mailassist --demo --naiv --datenfluss antworte 8     # a model that obeys injected instructions – the checks catch it
```

- **Data:** 48 synthetic mails – 21 hand-written B2B mails of the fictional "Beispiel Maschinenbau GmbH" (including 7 manipulation attempts) and 27 mails from Gretel's *synthetic_pii_finance_multilingual* dataset (Apache 2.0). Provenance and changes: [`src/mailassist/demo_data/NOTICE.md`](src/mailassist/demo_data/NOTICE.md).
- **Mock model:** rule-based and local – **not an AI**. It reads exactly the text a real model would receive. `--naiv` makes it obey instructions found in mails on purpose (links, discounts, "free of charge") to show the checks firing before approval.
- **Data-flow view** (`--datenfluss`): step by step, what was read, what was removed or defused, which approved facts were used, what was sent to the model and what was logged – in the terminal and as an HTML report under `data/demo/datenfluss/`.

## Results and limits

- **Live evaluation** (gpt-oss-120b on Bedrock, 21 synthetic mails, 26 Oct 2026): 76 % categorised correctly, 5 of 7 manipulation attempts detected; 2 drafts adopted injected discounts (30 %, 40 %) – both flagged as unbacked before approval. Second single run after adding the commitment check: 81 % correct, 4 of 7 detected; 1 draft with an injected 30 % discount and "delivery tomorrow" and 1 legitimate draft with an unapproved refund promise – both marked CRITICAL, saving only after confirmation.
- **Real mailbox** (29 Oct 2026): accepted against a GMX test mailbox with the real model – drafts filed as threaded replies, duplicate prompt and CRITICAL gate working, an injection test mail detected and ignored by the model. An independent read-only check on the server confirmed: nothing sent, nothing deleted or moved, mail read only by mailassist still unread.
- **The output check is context-free.** It finds numbers, links, addresses and percentages that are backed *nowhere*. A backed number used in the wrong context is not caught.
- **The commitment check uses keywords, not understanding.** Paraphrases without a keyword are missed; reported speech can be flagged as a promise. A keyword that also appears in the approved facts is not treated as proof – the draft stays silent only if the same figures are approved, otherwise the approved wording is shown for comparison.
- **Not tested yet:** a real Proton Mail Bridge connection, the T-Online server, live Telegram operation, a server installation.

## Development

```bash
uv run pytest                                  # all tests, offline, no real mailbox
MAILASSIST_LIVE=1 uv run pytest -m live -s     # optional live evaluation on the synthetic corpus (costs model calls)
```

Python 3.12 via uv. Tests use synthetic data only (`.example` domains). `tests/fake_baryon.py` is a real MCP server with trap tools (`send_email`, `save_draft`) that must never be called; `tests/fake_imap.py` has trap methods for destructive IMAP commands (`store`, `expunge`, `copy`, `move`, `delete`, `rename`, `create`, `subscribe`). Test plan (German): [docs/testplan.md](docs/testplan.md). Installation on a Linux server, Proton setup, knowledge-base format and troubleshooting: see the [German README](README.md).
