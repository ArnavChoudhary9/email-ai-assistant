# AI Workflow Project Spec: Local Email Intelligence Assistant

## 1. Project Overview

Build a **locally hosted autonomous AI workflow system** that continuously monitors multiple email inboxes, extracts actionable information, summarizes important content, sends alerts, and updates calendars automatically.

The system runs on the user's machine/server and integrates with:

* Gmail
* IMAP mail providers
* IITD Webmail (Roundcube / IMAP-backed if available)
* Telegram
* Slack
* Google Calendar
* OpenRouter LLM APIs
* Python runtime

Primary objective:

> Turn incoming email clutter into a clean daily operational assistant.

---

# 2. Core Goals

The system should:

1. Fetch emails from multiple accounts
2. Parse unread/new emails
3. Detect importance
4. Extract tasks, deadlines, meetings, forms, actions
5. Summarize intelligently
6. Notify via Telegram + Slack
7. Create / update Google Calendar events
8. Maintain memory/history locally
9. Avoid duplicate alerts
10. Run fully locally except API calls

---

# 3. Functional Scope

---

## 3.1 Email Sources

Support modular providers:

### Gmail

* Gmail API preferred
* OAuth2 authentication
* Fallback IMAP if needed

### Generic IMAP

* Any IMAP server
* SSL/TLS

### IITD Webmail

Likely Roundcube frontend over IMAP backend.

Need support for:

* IMAP direct login if credentials available
* Optional web scraping fallback (Playwright login automation) only if IMAP unavailable

---

## 3.2 Email Processing

For every new email:

Collect:

* Sender
* Subject
* Timestamp
* Body text
* Attachments metadata
* Thread references
* Labels/folder

Then classify:

* Critical
* Important
* Informational
* Promotional
* Spam/Ignore

---

## 3.3 AI Extraction Tasks

LLM should extract:

```json
{
  "summary": "",
  "importance": "critical|important|normal|ignore",
  "action_required": true,
  "deadline": "",
  "meeting": {
    "exists": false,
    "date": "",
    "time": "",
    "location": ""
  },
  "tasks": [],
  "calendar_events": [],
  "reply_needed": false,
  "reply_priority": ""
}
```

---

## 3.4 Notifications

### Telegram

Send concise high-value alerts:

```text
[IMPORTANT EMAIL]

From: Placement Cell IITD
Subject: Internship Interview Schedule

Summary:
Interview scheduled for Tuesday 3 PM.

Action:
Prepare documents.

Calendar added.
```

### Slack

Use structured formatting:

* Priority channels
* Threads
* Rich blocks

---

## 3.5 Calendar Automation

Create Google Calendar events for:

* Meetings
* Deadlines
* Exams
* Submission dates
* Interviews
* Calls
* Appointments

Include:

* Title
* Description
* Start/end time
* Reminder notifications
* Source email link/reference

---

# 4. System Architecture

```text
email-intel/
│── app.py
│── scheduler.py
│── config/
│── providers/
│   ├── gmail.py
│   ├── imap.py
│   ├── roundcube.py
│── pipeline/
│   ├── fetch.py
│   ├── parse.py
│   ├── classify.py
│   ├── summarize.py
│   ├── calendar.py
│   ├── notify.py
│── integrations/
│   ├── telegram.py
│   ├── slack.py
│   ├── google_calendar.py
│   ├── openrouter.py
│── storage/
│   ├── sqlite.py
│── prompts/
│── logs/
│── tests/
```

---

# 5. Recommended Tech Stack

## Backend

* Python 3.12+
* FastAPI (optional dashboard/API)
* APScheduler / Celery for jobs
* SQLAlchemy
* SQLite initially
* PostgreSQL later

## AI

* OpenRouter API

Suggested models:

* Claude Sonnet
* GPT-4o Mini
* Gemini Flash
* DeepSeek

Use cheaper models for triage, better models for extraction.

## Parsing

* BeautifulSoup
* email stdlib
* html2text
* dateparser

## Integrations

* python-telegram-bot
* slack_sdk
* Google API Python Client

---

# 6. Processing Pipeline

## Poll Cycle

Every 5 mins:

```text
1. Fetch unread/new emails
2. Deduplicate
3. Parse clean text
4. Run rules engine
5. If useful -> LLM extraction
6. Save structured output
7. Notify user
8. Create calendar event
9. Mark processed
```

---

# 7. Smart Importance Logic

Before calling LLM, use cheap local heuristics.

Examples:

### High Priority

* placement
* interview
* deadline
* exam
* professor
* payment due
* urgent
* action required

### Ignore

* newsletters
* promotions
* ads
* social updates

This reduces API cost.

---

# 8. Database Schema

## emails

```sql
id
provider
message_id
sender
subject
received_at
raw_hash
processed
importance
summary
created_at
```

## tasks

```sql
id
email_id
title
due_date
status
```

## calendar_events

```sql
id
email_id
google_event_id
created_at
```

## notifications

```sql
id
email_id
telegram_sent
slack_sent
```

---

# 9. Security Requirements

## Secrets

Use `.env`

```env
OPENROUTER_API_KEY=
GMAIL_CLIENT_ID=
GMAIL_SECRET=
TELEGRAM_BOT_TOKEN=
SLACK_BOT_TOKEN=
GOOGLE_CALENDAR_CREDENTIALS=
```

## Rules

* Never store passwords plaintext
* Encrypt tokens if possible
* Local-only DB
* Logs redact secrets

---

# 10. Prompt Design for AI

## Extraction Prompt

```md
You are an email executive assistant.

Analyze the email and return strict JSON:

- summary
- importance
- deadlines
- meeting info
- tasks
- reply needed

Focus on university, internship, academic, finance, and urgent matters.
Ignore marketing noise.
```

---

# 11. Config Driven Accounts

```yaml
accounts:
  - type: gmail
    email: personal@gmail.com

  - type: imap
    host: mail.example.com
    email: work@example.com

  - type: iitd
    method: imap
    email: user@iitd.ac.in
```

---

# 12. Telegram Message Rules

Only send if:

* Critical
* Important
* Has deadline
* Meeting scheduled
* Needs urgent reply

Batch normal emails into daily digest.

---

# 13. Slack Message Rules

Slack can receive:

* Daily summary
* Inbox report
* Pending actions
* Weekly analytics

---

# 14. Google Calendar Rules

Avoid duplicate events.

If similar event exists:

* update instead of create

Use fuzzy matching on:

* title
* date
* source email thread

---

# 15. Future Upgrades

## Dashboard

Web UI:

* inbox analytics
* missed deadlines
* reply queue
* AI confidence score

## Voice Assistant

“What important emails today?”

## Auto Reply Drafting

Generate suggested responses.

## RAG Search

Ask:

> Did professor send assignment date last month?

---

# 16. Development Phases

## Phase 1 MVP

* Gmail + IMAP fetch
* LLM summaries
* Telegram alerts

## Phase 2

* Slack
* Google Calendar
* Better extraction

## Phase 3

* IITD Roundcube automation
* Dashboard
* Memory + analytics

## Phase 4

* Auto replies
* Multi-user support

---

# 17. Engineering Rules

## Code Quality

* Strong modularity
* Provider pattern
* Retry system
* Logging
* Typed Python
* Unit tests
* Idempotent jobs

## Error Handling

If Gmail down:

* continue others

If OpenRouter fails:

* fallback summarizer

If Calendar fails:

* retry queue

---

# 18. Suggested Models Strategy

## Cheap Stage

Email triage:

* Gemini Flash / GPT Mini

## Expensive Stage

Important emails:

* Claude Sonnet

---

# 19. Success Metrics

* Important email detection accuracy
* Deadline extraction accuracy
* False alert rate
* Time saved weekly
* Calendar correctness

---

# 20. Final Mission Statement

Build a private AI chief-of-staff that transforms scattered emails into organized action automatically.

---

# 21. Immediate Build Order

```text
1. IMAP + Gmail fetchers
2. Email parser
3. SQLite state db
4. OpenRouter summarizer
5. Telegram notifier
6. Google Calendar sync
7. Slack digest
8. IITD Roundcube connector
```

---

# 22. Recommended Claude / AI Builder Prompt

```md
Build this project in Python with production-grade architecture.

Requirements:
- Modular provider system
- Clean codebase
- Async where useful
- SQLite initially
- Strong logging
- .env config
- Typed Python
- Telegram + Slack + Google Calendar integrations
- OpenRouter abstraction layer
- Retry queues
- Future scalable design

Start with Phase 1 MVP.
```
