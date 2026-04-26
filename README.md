# Financial Analysis Agent

A local financial analysis chat agent. Ask questions about company financials;
Claude answers only from SEC filings stored in a local Postgres database — no
live internet access in the chat path.

---

## Architecture

```
[Internet] → [Ingestion pipeline] → [Postgres + pgvector]
                                           ↑
                                           │ SELECT only
                                           │
     [User] ↔ [Chat backend] ↔ [Claude API (tool_use)]
```

Three isolated subsystems:

| Subsystem | Internet? | DB access | Code |
|-----------|-----------|-----------|------|
| Ingestion | Yes (SEC EDGAR) | Read-write (`finagent` user) | `app/ingestion/` |
| Database | — | — | Postgres 16 + pgvector |
| Chat backend | Anthropic API only | Read-only (`readonly` user) | `app/chat/` |

### Four-level retrieval hierarchy

Claude has five tools, each querying a different level of detail:

| Level | Table | Used for |
|-------|-------|----------|
| 1 | `document_summaries` | "Give me an overview of Apple" |
| 2 | `section_summaries` | Navigate to the right section |
| 3 | `document_chunks` | Find specific evidence (vector search) |
| 4 | `financial_line_items` | Exact figures — "What was revenue in Q3 2024?" |

---

## Prerequisites

- Docker + Docker Compose
- Anthropic API key (`ANTHROPIC_API_KEY`)
- Groq API key (`GROQ_API_KEY`) — for ingestion summaries
- SEC EDGAR email (`SEC_USER_AGENT_EMAIL`) — required by EDGAR, any real email

---

## Setup

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY, GROQ_API_KEY, SEC_USER_AGENT_EMAIL

# 2. Start Postgres
docker compose up postgres -d

# 3. Run migrations
docker compose run --rm backend alembic upgrade head

# 4. Start the backend
docker compose up backend -d
```

---

## Running ingestion

Ingestion fetches SEC filings and populates the database. Run it manually
(filings only appear ~4 times per year per company).

```bash
# Inside the backend container
docker compose exec backend python -m app.ingest --tickers AAPL --since 2023-01-01

# All configured companies
docker compose exec backend python -m app.ingest --all --since 2022-01-01

# Check ingestion status
docker compose exec backend python -m app.ingest --status

# Use LLM-generated section summaries (better quality, uses Groq API)
docker compose exec backend python -m app.ingest --tickers AAPL --since 2023-01-01 --summary-method llm
```

**Local dev** (connects to Docker postgres on localhost:5432):

```bash
# Install dependencies
poetry install --without ml,dev --no-root
pip install fastembed==0.3.6   # installed via pip — poetry can't resolve on Python 3.9

# Run ingestion locally
DATABASE_URL="postgresql://finagent:password@localhost:5432/finagent" \
  READONLY_DATABASE_URL="postgresql://readonly:password@localhost:5432/finagent" \
  SEC_USER_AGENT_EMAIL="your@email.com" \
  GROQ_API_KEY="gsk_..." \
  poetry run python -m app.ingest --tickers AAPL --since 2024-01-01
```

---

## Uploading custom documents

You can upload your own PDFs (analyst reports, research notes) via the API:

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@report.pdf" \
  -F "ticker=AAPL" \
  -F "label=Goldman Sachs AAPL Report Q1 2024"
```

Uploaded documents go through the same chunking + embedding + summarization
pipeline as SEC filings. They appear in chat results with `doc_type=UPLOAD`.

---

## Chat

```bash
# CLI chat (bypasses frontend)
docker compose exec backend python -m app.chat

# Web UI
open http://localhost:3000
```

---

## Adding companies

Edit `config.yaml`:

```yaml
companies:
  - ticker: NVDA
    name: NVIDIA Corporation
    cik: "0001045810"
```

Find the CIK at `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=nvidia`.

Then re-run ingestion:

```bash
docker compose exec backend python -m app.ingest --tickers NVDA --since 2022-01-01
```

---

## Environment variables

| Variable | Required for | Description |
|----------|-------------|-------------|
| `ANTHROPIC_API_KEY` | Chat (Phase 4+) | Claude Sonnet for chat |
| `GROQ_API_KEY` | Ingestion summaries | Groq qwen/qwen3-32b for doc summaries |
| `SEC_USER_AGENT_EMAIL` | Ingestion | Required by SEC EDGAR (any real email) |
| `DATABASE_URL` | Ingestion | Read-write Postgres URL |
| `READONLY_DATABASE_URL` | Chat backend | Read-only Postgres URL |

---

## Project structure

```
app/
  ingestion/
    edgar_client.py     # HTTP client: rate limit + User-Agent
    fetcher_xbrl.py     # Fetch XBRL companyfacts (Level 4)
    fetcher_filings.py  # Fetch 10-K/10-Q HTML
    parser_xbrl.py      # XBRL → financial_line_items
    parser_html.py      # HTML → clean sections
    chunker.py          # Sections → overlapping chunks
    embedder.py         # Text → 384-dim vectors (fastembed)
    summarizer.py       # Section + document summaries (Groq)
    parser_pdf.py       # PDF → sections (uploads)
    fetcher_upload.py   # Save uploaded files
    pipeline.py         # Orchestrator
    cli.py              # CLI entry point
  chat/
    tools.py            # 5 DB query tools
    agent.py            # Claude tool-use loop
    router.py           # FastAPI SSE endpoint
  migrations/           # Alembic
  main.py               # FastAPI app
frontend/               # React/Vite chat UI
config.yaml             # Companies + ingestion settings
```

---

## Tech stack

- Python 3.9+ + FastAPI
- Postgres 16 + pgvector (384-dim vectors)
- [fastembed](https://github.com/qdrant/fastembed) with `all-MiniLM-L6-v2` — ONNX, no torch required
- Groq API (`qwen/qwen3-32b`) for ingestion summaries
- Claude Sonnet for chat (Anthropic SDK with tool_use)
- React/Vite frontend
- Docker Compose
