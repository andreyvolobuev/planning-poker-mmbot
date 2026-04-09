# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Mattermost bot for planning poker (story point estimation) with optional Jira integration. Written in Python 3.

## Setup & Running

```bash
pip install -r requirements.txt
cp .env.example .env  # then edit .env with real credentials
python -m src.main
```

Required env vars: `MATTERMOST_URL`, `BOT_TOKEN`. Optional: `SESSION_STATE_PATH` (SQLite path for persistence; otherwise state is in-memory and lost on restart).

No test suite exists.

## Architecture

The bot connects to Mattermost via WebSocket and listens for `posted` events. All business logic lives in `src/handlers.py`.

**Module responsibilities:**
- `main.py` — entry point: loads config, authenticates, initializes storage, starts WebSocket listener, calls `sweep_all_voted_sessions()` on startup to finalize incomplete rounds
- `handlers.py` — all business logic: session creation, voting, commands, result calculation
- `sessions.py` — in-memory `SessionStore` with `PlanningSession` dataclass
- `session_sqlite.py` — `SqliteBackedSessionStore` extends `SessionStore` with SQLite persistence (WAL mode); loaded on startup, updated on each vote, removed on finalization
- `parsing.py` — regex-based Jira URL extraction, username parsing, vote parsing with decimal quantization (rounds up to nearest 0.05)
- `mattermost_client.py` — driver factory wrapping `mattermostdriver.Driver`
- `mattermost_websocket.py` — `ServerAuthSSLWebsocket` subclass fixing SSL context for outbound WSS connections (works around a driver bug)
- `jira_client.py` — Jira REST API integration for updating story points and time tracking
- `config.py` — `Settings` and `JiraIntegration` dataclasses loaded from environment

**Voting flow:**
1. Bot sees a root post (not a reply) in a team channel mentioning usernames → creates `PlanningSession`
2. Bot DMs each mentioned user with a link back to the thread
3. Users reply in their DM threads with numeric votes
4. When all users vote (or organizer uses `/finish`), bot posts median + mean results in the original channel thread
5. Organizer can use `/agree <SP>` to sync the decision to Jira

**Commands** (sent as replies in the original channel thread): `/finish`, `/list`, `/reset`, `/agree <SP>`

**Vote parsing:** accepts integers and decimals, both `.` and `,` as decimal separators; quantized to 0.05 steps (rounded up)

**Result calculation:** median (ceil for even count), arithmetic mean to 0.01 precision

**UI text is in Russian (Cyrillic).**

## Key Design Notes

- Single-threaded async event loop — no threading concerns within the bot
- Multiple concurrent sessions are supported per channel
- Sessions are keyed by the DM thread's root post ID
- If the bot crashes after posting results but before DB cleanup, results may be duplicated on next startup — requires manual DB cleanup
- The bot must be manually added to channels; it only handles team channel types O, P, G (not DM channels)
