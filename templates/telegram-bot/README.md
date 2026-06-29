# Standalone Telegram bot (zero-infra template)

A turnkey Telegram bot for Researchy: **drop in a token, run one command, done.**
It polls Telegram and runs the full four-agent pipeline *in-process* — the same
`--local` path the CLI uses — so there's **no Docker, API, Celery, Postgres, Redis,
or API key** to set up. Just LLM + Tavily keys.

> For the durable, multi-user path (many people attach their own bots through the
> API), see `research_assistant/bot/` and the `/bot/connect` endpoint instead. This
> template is the opposite end: one bot, one process, one command.

## Run it

```bash
# from the repo root, with the project installed (pip install -e ".[dev]")
cp templates/telegram-bot/.env.example templates/telegram-bot/.env
#   → fill in TELEGRAM_BOT_TOKEN, your LLM key, and TAVILY_API_KEY

python templates/telegram-bot/bot.py
```

That's it. Message the bot in Telegram — `/start` for a greeting, then send any
question. It replies `🔍 Researching…`, runs the pipeline, and edits that message
with the finished, sourced report.

## Config

All config lives in this folder's `.env` (see `.env.example`):

| Key                  | Required | Notes                                              |
| -------------------- | -------- | -------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | yes      | from [@BotFather](https://t.me/BotFather) → `/newbot` |
| `LLM_MODEL` + key    | yes      | any LiteLLM model string, e.g. `openai/gpt-4o`     |
| `TAVILY_API_KEY`     | yes      | web search                                         |
| `RESEARCH_DEPTH`     | no       | `quick` \| `standard` \| `deep` (default `standard`) |

## Limits (by design)

Because it runs `--local`, there's no persistence (history isn't saved) and no
live progress stream — you get `🔍 Researching…` → final report. Reports are
truncated to Telegram's 4096-char message limit. That's the right trade for a
one-command demo; reach for the API + `BotManager` path when you need durability.
