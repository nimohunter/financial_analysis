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
| `ANTHROPIC_API_KEY` | Chat backend | Claude Sonnet for chat |
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
- [fastembed](https://github.com/qdrant/fastembed) with `BAAI/bge-small-en-v1.5` — ONNX, no torch required (384-dim)
- Groq API (`qwen/qwen3-32b`) for ingestion summaries
- Claude Sonnet (`claude-sonnet-4-6`) for chat (Anthropic SDK with tool_use)
- React/Vite frontend
- Docker Compose

---

## Design analysis

### Strengths

- **Four-level retrieval hierarchy** keeps exact financial numbers (XBRL/SQL) separate from narrative passages (vectors). Most RAG systems flatten everything into one vector store; financial Q&A fails when Claude has to parse tables in chunks instead of querying structured data.
- **Context-enriched embeddings** — every chunk and summary is embedded as `"{company}, {doc_type}, {section_title}: {text}"` rather than raw text. Short texts embed poorly; prepending document context is a known fix that improves retrieval precision.
- **Extractive summaries by default, LLM upgrade path** — the `--summary-method llm` flag lets you upgrade section summary quality without changing the schema. Free on the first pass, better on the second.
- **Prompt caching on system prompt + tool definitions** — these tokens are constant across every chat turn. Caching them cuts per-turn cost and latency noticeably on Claude Sonnet.

### Known limitations and mitigations

**1. ~~`all-MiniLM-L6-v2` is a general-purpose model~~ — Fixed.**
Switched to `BAAI/bge-small-en-v1.5` in `embedder.py`. Same 384-dim output (zero
schema changes), trained on broader and more domain-diverse corpora. Better
coverage of financial language ("EBITDA margin", "covenant compliance", "liquidity risk").

**2. Extractive section summaries take the first sentence of every 3rd paragraph.**
MD&A sections typically bury key numbers mid-section. The first sentence is often
a backward-looking preamble, not the substance. L2 retrieval (`search_sections`)
will surface the wrong sections for specific financial queries.

*Recommendation*: Run ingestion with `--summary-method llm` after Phase 3c.
Groq `qwen/qwen3-32b` costs ~$0.001 per section — negligible.

```bash
python -m app.ingest --tickers AAPL --since 2024-01-01 --summary-method llm
```

**3. No reranking — retrieval is purely cosine distance top-k. — Implemented in Phase 4**
A query like "supply chain disruption risk" surfaces chunks sharing vocabulary but
not necessarily the most relevant evidence. Precision drops as the corpus grows.

`search_passages` uses hybrid search: dense vector (bge-small) + BM25 sparse
(fastembed `SparseTextEmbedding`) fused with Reciprocal Rank Fusion. No new packages;
fetches top-50 by dense distance, reranks by BM25, returns top-k.

**4. ~~History truncation cuts at 3 turns~~ — Fixed (Phase 4).**
Replaced with token-count truncation (20k-token budget for tool results). Financial
analysis queries often chain across many turns; a fixed turn cutoff breaks multi-step
reasoning. Oldest tool results are dropped first, replaced with a one-line placeholder.

**5. ~~`search_passages` takes a single `company_id`~~ — Fixed (Phase 4).**
Both `search_sections` and `search_passages` accept `company_ids: list[int]`
(SQL: `WHERE d.company_id = ANY(:ids)`). Cross-company questions ("compare Apple and
Microsoft's supply chain risk") resolve in a single tool call. The system prompt
instructs Claude to use this rather than making separate per-company calls.
