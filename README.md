# Email Intel — Local Email Intelligence Assistant

Phase 1 of the system described in [PRD.md](PRD.md). The agent:

1. **Polls IMAP inboxes** (Gmail via app password, any IMAP server).
2. **Filters** promotional noise locally with keyword heuristics before any LLM call.
3. **Extracts** summary, importance, tasks, deadlines, meetings via **OpenRouter** (Claude / GPT / Gemini — configurable).
4. **Creates Google Calendar events** for meetings, deadlines, and any explicit events the LLM surfaces.
5. **Sends a Telegram alert** with the summary + tasks + "Calendar added." line whenever something actionable lands.
6. **Persists state in SQLite** so the same email is never re-processed or re-alerted.

Runs fully locally. Cross-platform (Windows + Linux).

---

## Quickstart

This project was built against Python 3.11+ with a plain `venv`. No `uv` required.

### 1. Create the venv and install

```bash
python -m venv venv

# Windows
venv\Scripts\pip install -e .

# Linux / macOS
venv/bin/pip install -e .
```

### 2. Configure secrets

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Where to get it |
| --- | --- |
| `OPENROUTER_API_KEY` | <https://openrouter.ai/keys> |
| `TELEGRAM_BOT_TOKEN` | Talk to [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | Send your bot a message, then `curl https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id` |

Defaults that are fine to leave alone:

```env
EXTRACTION_MODEL=anthropic/claude-sonnet-4.5
FALLBACK_MODEL=openai/gpt-4o-mini
POLL_INTERVAL_MINUTES=5
GOOGLE_CALENDAR_ID=primary
```

### 3. Configure your inbox

```bash
cp config/accounts.example.yaml config/accounts.yaml
```

Edit `config/accounts.yaml`:

```yaml
accounts:
  - name: personal
    type: imap
    host: imap.gmail.com
    port: 993
    use_ssl: true
    email: you@example.com
    password: $IMAP_PERSONAL_PASSWORD    # or inline, ALWAYS QUOTED
    folder: INBOX
    initial_lookback_days: 3
```

Then export the password so the `$IMAP_PERSONAL_PASSWORD` ref resolves:

```bash
# Windows (PowerShell)
$env:IMAP_PERSONAL_PASSWORD = "your-app-password"

# Linux / macOS
export IMAP_PERSONAL_PASSWORD="your-app-password"
```

> **Gmail users**: use an App Password from <https://myaccount.google.com/apppasswords>, not your regular password.
>
> **YAML tip**: if you inline a password made of digits only, **quote it** (`password: "63973518"`). Unquoted digit strings get parsed as ints and lose leading zeros.

### 4. (Optional) Set up Google Calendar

If `config/google_client_secret.json` doesn't exist, calendar sync is silently skipped and the rest of the pipeline keeps working. To enable it:

1. Open <https://console.cloud.google.com/apis/credentials>.
2. **Create OAuth client ID** → Application type: **Desktop app**.
3. Enable the **Google Calendar API** for the same project (APIs & Services → Library → Google Calendar API → Enable).
4. Download the credentials JSON and save it to `config/google_client_secret.json`.
5. On the **first** run the app opens your browser to grant consent. A refresh token is cached at `data/google_token.json` so subsequent runs are silent.

Only calendar write scope (`calendar.events`) is requested.

### 5. Run it

```bash
# Windows
venv\Scripts\python -m email_intel.app        # one cycle, then exit
venv\Scripts\python -m email_intel            # forever, polls every POLL_INTERVAL_MINUTES

# Linux / macOS
venv/bin/python -m email_intel.app
venv/bin/python -m email_intel
```

The installed console scripts also work once the venv is activated:

```bash
email-intel-once     # one cycle
email-intel          # scheduler
```

---

## What happens each cycle

```
IMAP fetch
  → dedup against SQLite (message_id + raw_hash)
  → parse HTML / plain text
  → classify with keyword heuristics (skip newsletters, flag "deadline"/"interview"/...)
  → for important emails:
      → OpenRouter extract JSON (summary, tasks, deadlines, meeting, calendar_events)
      → create Google Calendar events (if configured), dedup per-email
      → send Telegram alert with summary + tasks + "Calendar added."
      → record notification so restarts don't double-alert
```

On failure (OpenRouter 5xx, Calendar API hiccup, Telegram outage), the cycle logs, records `last_error`, and moves on. Nothing blocks subsequent emails.

---

## Project layout

```
src/email_intel/
├── __main__.py                    # python -m email_intel → scheduler
├── app.py                         # one-shot cycle (python -m email_intel.app)
├── scheduler.py                   # APScheduler blocking loop
├── config.py                      # pydantic Settings + accounts.yaml
├── models.py                      # Email, Extraction, Classification
├── logging_setup.py               # rotating logs + secret redaction
├── providers/imap.py              # IMAP fetcher
├── pipeline/
│   ├── fetch.py                   # new-email iterator with DB dedup
│   ├── parse.py                   # html2text + whitespace normalization
│   ├── classify.py                # keyword heuristics gate
│   ├── summarize.py               # OpenRouter extraction + repair retry
│   ├── calendar.py                # build & sync Google Calendar events
│   └── notify.py                  # Telegram dispatch (idempotent)
├── integrations/
│   ├── openrouter.py              # httpx + tenacity retry + fallback model
│   ├── telegram.py                # sync POST to api.telegram.org
│   └── google_calendar.py         # OAuth installed-app flow
├── prompts/extraction.py          # LLM system prompt
└── storage/
    ├── db.py                      # SQLAlchemy engine + session_scope
    ├── schema.py                  # emails, tasks, notifications, calendar_events
    └── repo.py                    # CRUD helpers (is_seen, save_extraction, …)
```

---

## Testing

```bash
# Windows
venv\Scripts\python -m pytest

# Linux / macOS
venv/bin/python -m pytest
```

Tests use in-memory SQLite and stub OpenRouter / Telegram / Calendar — they run offline in under 15s. Strict type checking:

```bash
venv\Scripts\python -m mypy src/email_intel
```

---

## Troubleshooting

**`No endpoints found for <model>`** — the OpenRouter model slug in `.env` is retired. Browse <https://openrouter.ai/models> and pick a current one (e.g. `anthropic/claude-sonnet-4.5`, `openai/gpt-4o-mini`, `google/gemini-2.0-flash-001`).

**`Event loop is closed`** — should no longer happen; the Telegram client is now plain sync httpx. If you see it, the scheduler is likely sharing state across threads; open an issue with the trace.

**`Env var 'IMAP_PERSONAL_PASSWORD' referenced in accounts.yaml is empty`** — you put `password: $IMAP_PERSONAL_PASSWORD` in the YAML but didn't `export` the variable. Either export it in the same shell before running, or inline-quote the password in the YAML.

**Google Calendar browser consent opens every run** — the token file at `data/google_token.json` wasn't written. Check that `data/` is writable. The first run must be on a machine with a browser; after that you can copy `data/google_token.json` to a headless server.

---

## What's not in MVP

Gmail OAuth, Slack, IITD Roundcube, FastAPI dashboard, auto-reply drafting, multi-user, PostgreSQL, RAG search. These live in later phases per [PRD.md](PRD.md) §16.
