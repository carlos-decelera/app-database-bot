# app-database-bot

A Slack bot for the Decelera program that lets the team query the application database in plain Spanish. Ask a question in Slack, get a human-readable answer — no SQL required.

Powered by [Claude](https://anthropic.com) (natural language → SQL), [Supabase](https://supabase.com) (PostgreSQL), and [Slack Bolt](https://slack.dev/bolt-python/).

---

## How it works

1. A team member mentions the bot in a channel or sends it a DM.
2. Claude converts the Spanish question into a safe, read-only SQL query.
3. The query runs against the Supabase database via an `exec_sql` RPC function.
4. Claude summarises the results and posts a concise reply in Slack.

```
User: "@bot ¿Cuántos founders hay hoy en el programa?"
Bot:  "Hay *12* founders presentes en el programa hoy."
```

The bot handles date parsing in Spanish, graceful retries, and automatically splits long replies across multiple messages.

---

## Features

- **Natural language to SQL** — understands Decelera-specific vocabulary (founders, experience makers, EMs, team)
- **Read-only by design** — only `SELECT` and `WITH...SELECT` queries are allowed; any write operation is blocked
- **Spanish date parsing** — resolves expressions like "25 de mayo", "mañana", "día 3" into ISO dates before querying
- **Automatic retries** — retries with an explicit date literal if the first attempt fails; falls back to queries without `unaccent()` if the extension is unavailable
- **Result pagination** — caps results at 20–50 rows and reports the true total count when results are truncated
- **Works in channels and DMs** — responds to `@mentions` and direct messages

---

## Requirements

- Python 3.11+
- A Supabase project with the `exec_sql` RPC function and `unaccent` extension enabled
- A Slack app with `app_mentions:read`, `chat:write`, `im:history`, and `reactions:write` scopes
- An Anthropic API key

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Decelera-Programs/app-database-bot.git
cd app-database-bot
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file (see `.env.example` if present) or set these in your deployment environment:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key |
| `SUPABASE_URL` | ✅ | Supabase project URL |
| `SUPABASE_KEY` | ✅ | Supabase service role key |
| `SLACK_BOT_TOKEN` | ✅ | Slack bot OAuth token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | ✅ | Slack app signing secret |
| `CLAUDE_MODEL` | ☑️ | Claude model to use (default: `claude-sonnet-4-20250514`) |
| `TIMEZONE` | ☑️ | Timezone for date queries (default: `Europe/Madrid`) |

### 3. Run locally

```bash
python main.py
```

The Flask server starts on port `3000` by default (or `$PORT` if set).

Point your Slack app's Event Subscriptions to `https://<your-host>/slack/events`.

---

## Deployment

The app is designed to run on [Railway](https://railway.app). Set the environment variables in the Railway project dashboard and deploy from this repo. The `/health` endpoint returns `{"status": "ok"}` and can be used as a health check.

---

## Database schema

The bot is aware of the following tables in the `public` schema:

| Table | Description |
|---|---|
| `Person` | Program participants (founders, experience makers, team) |
| `Startup` | Participating startups |
| `Event` | Program events and sessions |
| `UserEvent` | Event attendance/registration |
| `OneOnOne` | 1:1 sessions between experience makers and founders |
| `OneOnOneAudioSubmission` | Audio recordings from 1:1 sessions |
| `Notification` | Individual push notifications |
| `NotificationCampaign` | Bulk notification campaigns |
| `NotificationCampaignRecipient` | Campaign recipients and delivery status |
| `PushSubscription` | Device push subscriptions |
| `home_daily_content` | Daily content for the mobile app |

---

## Example questions

```
¿Cuántos founders hay hoy en el programa?
¿Qué eventos hay mañana?
¿Quiénes son los experience makers esta semana?
Lista todos los founders del sector fintech.
¿Cuántas sesiones 1:1 se han hecho esta semana?
```

---

## Project structure

```
app-database-bot/
├── main.py           # All application logic
├── requirements.txt  # Python dependencies
├── .gitignore
└── .claude/          # Claude Code configuration
```
