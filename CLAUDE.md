# Financial Analysis Agent — CLAUDE.md

## What this project is

A local financial analysis chat agent. Users ask questions about company
financials; Claude answers using only data fetched from SEC EDGAR and stored
in a local Postgres database. No live internet access in the chat path.

## Architecture (three isolated subsystems)

```
[Internet] → [Ingestion pipeline] → [Postgres + pgvector]
                                           ↑
                                           │ SELECT only
                                           │
     [User] ↔ [Chat backend] ↔ [Claude API (tool_use)]
```

- **Ingestion** (`app/ingestion/`) — only component with internet access.
  Fetches SEC EDGAR, parses, embeds, writes to DB.
- **Postgres** — shared data tier. Two DB users: `finagent` (read-write for
  ingestion), `readonly` (SELECT only for chat backend).
- **Chat backend** (`app/chat/`) — FastAPI + Claude API with tool_use. DB
  connection is read-only. No code path from chat to the internet except
  the Anthropic API.

## Four-level retrieval hierarchy

Claude has five tools corresponding to four data abstraction levels:

| Level | Table | Granularity | Use case |
|-------|-------|-------------|----------|
| 1 | `document_summaries` | One paragraph per filing | "How is Apple positioned overall?" |
| 2 | `section_summaries` | 2-3 sentences per section | Navigate to the right section |
| 3 | `document_chunks` | ~800-token passages + embeddings | Get citable evidence |
| 4 | `financial_line_items` | Exact XBRL figures | "What was revenue in Q3 2024?" |

## Tech stack

- Python 3.12 + FastAPI
- Postgres 16 + pgvector (384-dim vectors from `all-MiniLM-L6-v2`)
- Claude Sonnet for chat, Claude Haiku for ingestion summaries
- APScheduler for hourly ingestion in the worker container
- React/Vite frontend (minimal, no auth)
- Alembic for DB migrations
- Docker Compose for everything

## Key files

```
config.yaml              # companies (ticker, CIK) + ingestion settings
.env                     # secrets — copy from .env.example
docker-compose.yml
app/
  ingestion/             # EDGAR fetch → parse → embed → summarize
    edgar_client.py      # shared HTTP client: rate limit + User-Agent
    fetcher_xbrl.py      # fetch companyfacts JSON (Level 4)
    fetcher_filings.py   # find + download 10-K/10-Q HTML
    parser_xbrl.py       # XBRL → financial_line_items
    parser_html.py       # HTML → clean sections
    chunker.py           # sections → overlapping chunks
    embedder.py          # text → 384-dim vectors
    summarizer.py        # section + document summaries
    pipeline.py          # orchestrator
    cli.py               # CLI entry point
    scheduler.py         # APScheduler hourly job
  chat/
    tools.py             # 5 tool functions (DB queries)
    agent.py             # Claude tool-use loop
    router.py            # FastAPI SSE endpoint
  migrations/            # Alembic
  main.py                # FastAPI app entry point
frontend/                # React/Vite
```

## Development commands

```bash
# First-time setup
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY and SEC_USER_AGENT_EMAIL

# Start everything
docker compose up

# Run ingestion manually (inside ingestion container or with local venv)
python -m app.ingest --tickers AAPL --since 2023-01-01

# Chat CLI (bypasses frontend)
python -m app.chat

# Smoke test
make smoke
```

## DB users

- `finagent` — read-write, used by ingestion pipeline
- `readonly` — SELECT only, used by chat backend

Both are created automatically by `db/init.sql` on first `docker compose up`.

## EDGAR compliance rules

- Every HTTP request must include `User-Agent: FinAgent <SEC_USER_AGENT_EMAIL>`
- Max 10 requests/second (token-bucket rate limiter in `edgar_client.py`)
- Retry on 429/5xx: exponential backoff 1s → 2s → 4s, max 3 attempts

## Code conventions

- Max 300 lines per file — split into modules if needed
- Type hints on all Python functions
- Pydantic models for all tool inputs/outputs
- `logging` module only — no `print()`
- Every external call (EDGAR, Claude API) has retry with exponential backoff

## Environment variable reference

| Variable | Used by | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | chat backend | Claude API auth |
| `SEC_USER_AGENT_EMAIL` | ingestion | Required by SEC EDGAR |
| `DATABASE_URL` | ingestion | Read-write Postgres URL |
| `READONLY_DATABASE_URL` | chat backend | Read-only Postgres URL |
