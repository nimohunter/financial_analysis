# Build: Financial Analysis Agent (Demo)

Build a working demo of a financial analysis chat agent. Users chat with
Claude about company financials. Claude answers ONLY from a local database
— no live internet access in the chat path. A separate background pipeline
fetches and ingests data from SEC EDGAR.

---

## Scope

- 3–5 US public companies (start with AAPL, MSFT, GOOGL — configurable in
  `config.yaml`)
- Filing types: 10-K and 10-Q from SEC EDGAR
- Simple web UI for chat with streaming and citations
- Ingestion runs on-demand via CLI (no background scheduler — filings
  only appear ~4 times per year per company, so a cron or manual run is
  sufficient for the demo)
- Everything runs locally with `docker compose up`

---

## Architecture (non-negotiable constraints)

Three isolated subsystems sharing one Postgres database:

```
[Internet] → [Ingestion pipeline] → [Postgres + pgvector]
                                           ↑
                                           │ read-only
                                           │
     [User] ↔ [Chat backend] ↔ [Claude API with tool_use]
```

1. **Ingestion pipeline** — ONLY component with internet access. Fetches
   from EDGAR, parses, normalizes, generates summaries, embeds, writes to
   DB.
2. **Data tier** — Postgres 16 with pgvector. Structured financials AND
   vector chunks AND summaries in the same DB instance.
3. **Chat layer** — web UI → backend → Claude API with tools. The backend's
   DB user has SELECT-only privileges. No code path from the chat to the
   internet except the Anthropic API endpoint.

Enforce isolation at the DB level: create a `readonly_user` with
`GRANT SELECT ON ALL TABLES`. The chat backend connects as this user.

---

## Tech stack

Pick sensible defaults. Suggestions (override if you have a strong reason,
but document why in the README):

- **Backend**: Python 3.12 + FastAPI
- **Database**: Postgres 16 + pgvector extension
- **Embeddings**: sentence-transformers `all-MiniLM-L6-v2` (384 dimensions)
- **LLM for summaries**: Claude Haiku (claude-haiku-4-5-20241022) via the
  Anthropic SDK — used only during ingestion, not in the chat path
- **Chat model**: Claude Sonnet (claude-sonnet-4-5-20241022) via Anthropic
  SDK with tool_use
- **Frontend**: Minimal React (Vite) or plain HTML+JS — whichever is faster
  to get running. No auth needed.
- **Containerization**: docker-compose.yml with postgres and backend.
  Ingestion runs via `docker compose exec backend python -m app.ingest ...`
  — no separate worker service needed.

---

## Database schema

Use Alembic for migrations. Seed `companies` from `config.yaml`.

```sql
-- Core entity
companies (
    company_id   SERIAL PRIMARY KEY,
    ticker       VARCHAR(10) UNIQUE NOT NULL,
    name         VARCHAR(255) NOT NULL,
    cik          VARCHAR(10) NOT NULL,     -- SEC CIK number, zero-padded to 10 digits
    exchange     VARCHAR(20),
    sector       VARCHAR(100),
    currency     VARCHAR(3) DEFAULT 'USD',
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- Filing metadata
documents (
    document_id  SERIAL PRIMARY KEY,
    company_id   INT REFERENCES companies(company_id),
    doc_type     VARCHAR(10) NOT NULL,     -- '10-K', '10-Q'
    period_end   DATE NOT NULL,
    filed_at     TIMESTAMPTZ,
    source_url   TEXT,
    raw_hash     VARCHAR(64) UNIQUE,       -- sha256 for dedup
    status       VARCHAR(20) DEFAULT 'fetched',
                 -- fetched → parsed → normalized → indexed
    ingested_at  TIMESTAMPTZ DEFAULT now()
);

-- LEVEL 4: Structured financial data (no embedding)
financial_line_items (
    id               SERIAL PRIMARY KEY,
    company_id       INT REFERENCES companies(company_id),
    document_id      INT REFERENCES documents(document_id),
    statement_type   VARCHAR(20) NOT NULL,  -- 'income', 'balance', 'cashflow'
    period_end       DATE NOT NULL,
    period_type      VARCHAR(5) NOT NULL,   -- 'FY', 'Q'
    line_item        VARCHAR(100) NOT NULL, -- normalized canonical name
    value            NUMERIC,
    currency         VARCHAR(3) DEFAULT 'USD',
    unit             VARCHAR(20),           -- 'USD', 'shares', 'per_share'
    as_reported_label VARCHAR(255),         -- original label from filing
    UNIQUE (company_id, period_end, period_type, line_item, statement_type)
);

-- LEVEL 3: Passage chunks with embeddings
document_chunks (
    chunk_id         SERIAL PRIMARY KEY,
    document_id      INT REFERENCES documents(document_id),
    chunk_index      INT NOT NULL,
    section_title    VARCHAR(255),          -- 'ITEM 1A. RISK FACTORS'
    text             TEXT NOT NULL,          -- clean text for Claude to read
    embedding_input  TEXT,                   -- context-enriched text that was embedded
    embedding        vector(384) NOT NULL,   -- MiniLM-L6-v2 output
    token_count      INT
);

-- LEVEL 2: Section summaries with embeddings
section_summaries (
    summary_id       SERIAL PRIMARY KEY,
    document_id      INT REFERENCES documents(document_id),
    section_title    VARCHAR(255) NOT NULL,
    summary_text     TEXT NOT NULL,          -- 2-3 sentence summary
    summary_embedding vector(384) NOT NULL,
    chunk_start_index INT,                   -- links to first chunk in section
    chunk_end_index   INT                    -- links to last chunk in section
);

-- LEVEL 1: Document-level summaries with embeddings
document_summaries (
    summary_id       SERIAL PRIMARY KEY,
    document_id      INT REFERENCES documents(document_id) UNIQUE,
    summary_text     TEXT NOT NULL,          -- 1 paragraph overview
    summary_embedding vector(384) NOT NULL,
    key_themes       TEXT[]                  -- ['supply_chain', 'competition', ...]
);

-- Indexes
CREATE INDEX ON document_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX ON section_summaries USING ivfflat (summary_embedding vector_cosine_ops)
    WITH (lists = 50);
CREATE INDEX ON document_summaries USING ivfflat (summary_embedding vector_cosine_ops)
    WITH (lists = 20);
CREATE INDEX ON financial_line_items (company_id, statement_type, period_end);
CREATE INDEX ON document_chunks (document_id, section_title);
```

---

## Four-level retrieval hierarchy

This is the core design. Different questions need different levels of
detail. The system stores data at four abstraction levels and exposes a
tool at each level. Claude decides which level(s) to query.

### Level 1 — Document summaries (broadest)
- **What it stores**: 1 paragraph per filing. Key themes, overall picture.
- **When Claude uses it**: "How is Apple positioned overall?" / "Compare
  Apple and Microsoft"
- **Generation**: During ingestion, after section summaries exist.
  Summarize the section summaries with Claude Haiku into one paragraph.
- **Embedding**: Embed `"{company} {doc_type} {period}: {summary_text}"`
  (context-enriched). 384-dim vector.
- **Size**: ~100 tokens per summary. 5 filings = ~500 tokens. Small enough
  to pass several into Claude's context.

### Level 2 — Section summaries (navigational)
- **What it stores**: 2-3 sentences per section (Risk Factors, MD&A, etc.)
- **When Claude uses it**: To decide WHICH sections to drill into, or to
  answer cross-section comparison questions.
- **Generation**: During ingestion. Start with extractive approach (first
  sentence of each major paragraph in the section). Upgrade to LLM-
  generated summaries (Claude Haiku) later if retrieval quality is poor.
- **Embedding**: Embed `"{company} {doc_type} {period} {section_title}:
  {summary_text}"`. Short texts embed worse — the prepended context helps.
- **Size**: ~50-80 tokens each. 4-8 per filing.

### Level 3 — Passage chunks (evidence)
- **What it stores**: The actual paragraphs from the filing. ~800 tokens,
  100-token overlap, boundary-aware.
- **When Claude uses it**: To find specific evidence, answer detailed
  questions, get citable text.
- **Chunking rules** (in priority order):
  1. Never cross section boundaries
  2. Prefer paragraph boundaries over mid-paragraph splits
  3. Fall back to sentence boundaries if a single paragraph > 800 tokens
- **Embedding**: Embed `"{company} {doc_type} {section_title}: {chunk_text}"`
  — the stored `text` column has clean text (no metadata prefix), but the
  `embedding_input` column has the context-enriched version. Use the
  enriched version for embedding, the clean version for Claude to read.
- **Size**: ~800 tokens each. A 10-K might produce ~200 chunks.

### Level 4 — Structured data (exact numbers)
- **What it stores**: Financial line items with exact values. SQL, no
  vectors.
- **When Claude uses it**: "What was Apple's revenue in Q3 2024?" / "Show
  me the income statement."
- **Parsing source**: XBRL from EDGAR companyfacts API.
- **No embedding needed**. Exact match on company + period + line_item.
- **Taxonomy**: Maintain a mapping dict (~50 common items) from
  as_reported_label to canonical line_item name.

---

## Ingestion pipeline — detailed implementation spec

The ingestion pipeline is the hardest engineering in the system. This
section specifies every step: what to fetch, how to parse it, how to
transform it into each of the four retrieval levels, and what edge cases
to handle. Claude Code should implement these as separate modules in
`app/ingestion/` with one module per stage.

### Module structure
```
app/ingestion/
    __init__.py
    config.py           # EDGAR URLs, rate limits, taxonomy mapping
    edgar_client.py     # HTTP client with rate limiting + User-Agent
    fetcher_xbrl.py     # Step 1: fetch companyfacts JSON
    fetcher_filings.py  # Step 2: find and fetch filing HTML
    parser_xbrl.py      # Step 3: XBRL → financial_line_items
    parser_html.py      # Step 4: filing HTML → clean sections
    chunker.py          # Step 5: sections → chunks
    embedder.py         # Step 6: text → vectors
    summarizer.py       # Step 7: generate section + doc summaries
    pipeline.py         # Orchestrator: runs steps in order per company
    cli.py              # CLI entry point
```

### EDGAR compliance (applies to ALL steps that hit SEC servers)
- **User-Agent**: Every HTTP request must include the header
  `User-Agent: {app_name} {SEC_USER_AGENT_EMAIL}` — e.g.
  `FinAgent admin@example.com`. SEC blocks requests without this.
- **Rate limit**: Max 10 requests/second. Implement as a token-bucket
  rate limiter in `edgar_client.py`. All fetcher modules share one client
  instance.
- **Retry**: On 429 (rate limited) or 5xx, retry with exponential backoff
  (1s, 2s, 4s). Max 3 retries. On 403 with no User-Agent, log a clear
  error message telling the developer to set SEC_USER_AGENT_EMAIL.
- **Base URL**: `https://data.sec.gov` for APIs, `https://www.sec.gov` for
  filing archives.

---

### Step 1 — Fetch XBRL companyfacts (→ Level 4 structured data)

**What**: Download the companyfacts JSON for each company. This is one
large JSON file (~5-15MB) that contains ALL XBRL-reported facts for all
periods.

**URL pattern**:
```
https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
```
where `{cik}` is the 10-digit zero-padded CIK from config.yaml.

**Example**: For Apple (CIK 0000320193):
```
https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json
```

**Response structure** (simplified):
```json
{
  "cik": 320193,
  "entityName": "Apple Inc.",
  "facts": {
    "us-gaap": {
      "Revenue": {
        "label": "Revenue",
        "units": {
          "USD": [
            {
              "end": "2024-09-28",
              "val": 391035000000,
              "accn": "0000320193-24-000123",
              "fy": 2024,
              "fp": "FY",
              "form": "10-K",
              "filed": "2024-11-01"
            },
            {
              "end": "2024-06-29",
              "val": 85777000000,
              "accn": "0000320193-24-000098",
              "fy": 2024,
              "fp": "Q3",
              "form": "10-Q",
              "filed": "2024-08-02"
            }
          ]
        }
      },
      "NetIncomeLoss": { ... },
      "Assets": { ... }
    }
  }
}
```

**Dedup**: Hash the raw JSON bytes (sha256). Store in documents table with
`doc_type='XBRL-companyfacts'`. If hash matches existing row, skip parsing.

**What to extract**: Only the `us-gaap` namespace (ignore `dei`, `ifrs`,
custom namespaces). Within us-gaap, only the concepts in your taxonomy
mapping.

---

### Step 2 — Parse XBRL facts into financial_line_items (Level 4)

**Taxonomy mapping**: Maintain a Python dict that maps XBRL concept names
to your canonical line_item names. Start with these ~40 concepts:

```python
XBRL_TAXONOMY = {
    # Income statement
    "Revenues": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "SalesRevenueNet": "revenue",
    "CostOfGoodsAndServicesSold": "cost_of_revenue",
    "CostOfRevenue": "cost_of_revenue",
    "GrossProfit": "gross_profit",
    "ResearchAndDevelopmentExpense": "research_and_development",
    "SellingGeneralAndAdministrativeExpense": "selling_general_admin",
    "OperatingIncomeLoss": "operating_income",
    "InterestExpense": "interest_expense",
    "IncomeTaxExpenseBenefit": "income_tax",
    "NetIncomeLoss": "net_income",
    "EarningsPerShareBasic": "eps_basic",
    "EarningsPerShareDiluted": "eps_diluted",

    # Balance sheet
    "Assets": "total_assets",
    "AssetsCurrent": "current_assets",
    "CashAndCashEquivalentsAtCarryingValue": "cash_and_equivalents",
    "ShortTermInvestments": "short_term_investments",
    "AccountsReceivableNetCurrent": "accounts_receivable",
    "InventoryNet": "inventory",
    "Liabilities": "total_liabilities",
    "LiabilitiesCurrent": "current_liabilities",
    "LongTermDebtNoncurrent": "long_term_debt",
    "StockholdersEquity": "stockholders_equity",
    "RetainedEarningsAccumulatedDeficit": "retained_earnings",

    # Cash flow
    "NetCashProvidedByUsedInOperatingActivities": "operating_cash_flow",
    "NetCashProvidedByUsedInInvestingActivities": "investing_cash_flow",
    "NetCashProvidedByUsedInFinancingActivities": "financing_cash_flow",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capex",
    "PaymentsOfDividends": "dividends_paid",
    "PaymentsForRepurchaseOfCommonStock": "share_buybacks",
}
```

**How to classify statement_type**: Infer from the concept name pattern.
Alternatively, maintain a dict mapping each canonical name to its statement
type:
```python
STATEMENT_TYPES = {
    "revenue": "income",
    "net_income": "income",
    "total_assets": "balance",
    "operating_cash_flow": "cashflow",
    # etc.
}
```

**Period type mapping**: The companyfacts JSON uses `fp` field:
- `"FY"` → period_type = `"FY"`
- `"Q1"`, `"Q2"`, `"Q3"`, `"Q4"` → period_type = `"Q"`

**Filtering by form type**: Only ingest facts from `form` = `"10-K"` or
`"10-Q"`. Skip `"8-K"`, `"20-F"`, etc.

**Filtering by date**: Only ingest facts where `end` >= the `--since`
parameter.

**Unit handling**: Most values are in `USD`. EPS values are in
`USD/shares`. The `units` key in the JSON tells you which:
- `"USD"` → unit = `"USD"`, value as-is
- `"USD/shares"` → unit = `"per_share"`, value as-is
- `"shares"` → unit = `"shares"`, value as-is

**Value normalization**: The values in companyfacts are in raw units (not
thousands or millions). `391035000000` means $391 billion. Store the raw
value. Let the tool format it for Claude.

**Dedup at insert**: Use `INSERT ... ON CONFLICT (company_id, period_end,
period_type, line_item, statement_type) DO NOTHING`. This is the
idempotency guarantee for structured data.

**Edge case — duplicate concepts**: A company might report both `Revenues`
and `RevenueFromContractWithCustomerExcludingAssessedTax`. Both map to
`"revenue"`. Take the one from the more recent filing (higher `filed`
date). If same filing, prefer the concept that appears first in the
taxonomy dict.

**Edge case — amended filings**: The same period might appear multiple
times with different `accn` (accession numbers). The later one is an
amendment. Sort by `filed` date descending and take the first for each
unique (end, fp) pair.

---

### Step 3 — Find and fetch filing HTML documents (→ Levels 1-3)

**What**: For each company, find the actual 10-K and 10-Q HTML filing
documents on EDGAR, then download them.

**Step 3a — Get the filing index**:
Use the EDGAR submissions API to find filings:
```
https://data.sec.gov/submissions/CIK{cik}.json
```

This returns recent filings metadata. The `filings.recent` object has
arrays: `form`, `filingDate`, `accessionNumber`, `primaryDocument`. Filter
to `form` in `["10-K", "10-Q"]` and `filingDate` >= `--since`.

For each matching filing, construct the document URL:
```
https://www.sec.gov/Archives/edgar/data/{cik_no_leading_zeros}/{accession_no_dashes}/{primaryDocument}
```

where `accession_no_dashes` removes dashes from the accession number
(e.g., `0000320193-24-000123` → `000032019324000123`).

**Step 3b — Download the HTML**:
Fetch each filing HTML. These can be 500KB-2MB. Store the raw bytes for
hashing but don't persist them to disk — parse immediately.

**Dedup**: sha256 of raw HTML bytes. Check against `documents.raw_hash`.
If exists, skip. Otherwise insert a new `documents` row with status =
`'fetched'`.

**Edge case — filing packages**: Some filings are multi-file. The
`primaryDocument` is usually the main filing document. If it's an index
page (contains links to multiple files), take the largest HTML file in the
package — that's usually the 10-K/10-Q itself.

**Edge case — older filings**: Before ~2020, some filings use plain text
(.txt) format instead of HTML. For the demo, skip these — only ingest
HTML filings. Log a warning and continue.

---

### Step 4 — Parse filing HTML into clean sections

**What**: Take the raw HTML and extract clean, structured text organized by
section headings (ITEM 1, ITEM 1A, ITEM 7, etc.).

**Step 4a — Strip HTML boilerplate**:
Use BeautifulSoup to:
1. Remove `<script>`, `<style>`, `<meta>`, `<link>` tags
2. Remove XBRL inline tags (`<ix:*>`) but keep their text content
3. Remove page headers/footers (often in `<div>` elements with fixed
   positioning or specific class names like `"pageHeader"`)
4. Convert `<br>` to newlines
5. Convert `<p>` to double newlines
6. Collapse multiple whitespace/newlines to max 2 consecutive newlines
7. Strip leading/trailing whitespace from every line

**Step 4b — Detect section boundaries**:
SEC 10-K filings follow a standard structure. Detect sections by matching
heading patterns. Use regex on the cleaned text:

```python
SECTION_PATTERNS = [
    # 10-K sections
    (r"(?i)ITEM\s+1\.?\s*[-–—.]?\s*BUSINESS", "ITEM 1. BUSINESS"),
    (r"(?i)ITEM\s+1A\.?\s*[-–—.]?\s*RISK\s+FACTORS", "ITEM 1A. RISK FACTORS"),
    (r"(?i)ITEM\s+1B\.?\s*[-–—.]?\s*UNRESOLVED\s+STAFF", "ITEM 1B. UNRESOLVED STAFF COMMENTS"),
    (r"(?i)ITEM\s+2\.?\s*[-–—.]?\s*PROPERTIES", "ITEM 2. PROPERTIES"),
    (r"(?i)ITEM\s+3\.?\s*[-–—.]?\s*LEGAL\s+PROCEEDINGS", "ITEM 3. LEGAL PROCEEDINGS"),
    (r"(?i)ITEM\s+5\.?\s*[-–—.]?\s*MARKET", "ITEM 5. MARKET FOR REGISTRANT'S COMMON EQUITY"),
    (r"(?i)ITEM\s+6\.?\s*[-–—.]?\s*(?:SELECTED|RESERVED)", "ITEM 6. RESERVED"),
    (r"(?i)ITEM\s+7\.?\s*[-–—.]?\s*MANAGEMENT", "ITEM 7. MD&A"),
    (r"(?i)ITEM\s+7A\.?\s*[-–—.]?\s*QUANTITATIVE", "ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES"),
    (r"(?i)ITEM\s+8\.?\s*[-–—.]?\s*FINANCIAL\s+STATEMENTS", "ITEM 8. FINANCIAL STATEMENTS"),
    (r"(?i)ITEM\s+9\.?\s*[-–—.]?\s*CHANGES\s+IN", "ITEM 9. CHANGES IN AND DISAGREEMENTS"),
    (r"(?i)ITEM\s+9A\.?\s*[-–—.]?\s*CONTROLS", "ITEM 9A. CONTROLS AND PROCEDURES"),

    # 10-Q sections
    (r"(?i)PART\s+I.*FINANCIAL\s+INFORMATION", "PART I. FINANCIAL INFORMATION"),
    (r"(?i)PART\s+II.*OTHER\s+INFORMATION", "PART II. OTHER INFORMATION"),
    (r"(?i)ITEM\s+1\.?\s*[-–—.]?\s*FINANCIAL\s+STATEMENTS", "ITEM 1. FINANCIAL STATEMENTS (10-Q)"),
    (r"(?i)ITEM\s+2\.?\s*[-–—.]?\s*MANAGEMENT.*DISCUSSION", "ITEM 2. MD&A (10-Q)"),
    (r"(?i)ITEM\s+3\.?\s*[-–—.]?\s*QUANTITATIVE", "ITEM 3. QUANTITATIVE DISCLOSURES (10-Q)"),
]
```

Walk through the clean text. When a line matches a section pattern, start
a new section. Everything between two headings belongs to the first
heading's section.

Return a `list[Section]`:
```python
@dataclass
class Section:
    title: str          # normalized title, e.g. "ITEM 1A. RISK FACTORS"
    text: str           # all text in this section
    start_char: int     # offset in full document (for debugging)
    end_char: int
```

**Which sections to keep for narrative (Levels 1-3)**:
Not all sections have useful narrative text. Keep:
- ITEM 1. BUSINESS (company overview)
- ITEM 1A. RISK FACTORS (critical)
- ITEM 7. MD&A (critical — management's explanation of financials)
- ITEM 7A. QUANTITATIVE DISCLOSURES (market risk)
- ITEM 2. MD&A (10-Q version)

Skip or deprioritize:
- ITEM 8. FINANCIAL STATEMENTS (tables — use XBRL data in Level 4 instead)
- ITEM 2. PROPERTIES (short, rarely queried)
- ITEM 9/9A. CONTROLS (procedural boilerplate)

Store a config set `NARRATIVE_SECTIONS` that lists which sections to chunk
and embed. Others are still parsed (for completeness) but only stored as
section summaries (Level 2), not chunked (Level 3).

**Step 4c — Handle tables inside narrative sections**:
Tables appear frequently inside Risk Factors and MD&A. Two cases:

1. **Small table** (< 300 tokens after text extraction): Flatten to text
   and include in the surrounding paragraph flow. Format as:
   ```
   | Header1 | Header2 | Header3 |
   | val1    | val2    | val3    |
   ```

2. **Large table** (>= 300 tokens): Extract as a separate chunk with
   `chunk_type = "table"`. Don't split it — keep the table intact as one
   chunk regardless of size. Tag it with the section_title it belongs to.

Detect tables by looking for `<table>` tags in the HTML BEFORE stripping
(save table boundaries during Step 4a). For each table, use BeautifulSoup
to extract `<tr>`/`<th>`/`<td>` into pipe-delimited text.

**Edge case — table of contents**: Many filings start with a table of
contents that's a clickable list of section headings. Detect it (usually
the first `<table>` or a block of text with "Table of Contents" heading)
and skip it entirely.

**Edge case — section not found**: Some filings use non-standard headings
or split sections across multiple HTML files. If no sections are detected,
log a warning and treat the entire document as one section titled
"UNSTRUCTURED". Still chunk and embed it — better to have imprecise
section metadata than to lose the text entirely.

Update document status to `'parsed'` after this step.

---

### Step 5 — Chunk sections into passages (→ Level 3)

**What**: Split each section's text into overlapping chunks of ~800 tokens,
respecting paragraph and sentence boundaries.

**Tokenization**: Use a fast tokenizer for counting only — don't embed at
this stage. `tiktoken` with the `cl100k_base` encoding is fine for token
counting (it's close enough to what MiniLM would count). Or simply use
`len(text) / 4` as an approximation (1 token ≈ 4 chars for English).

**Algorithm** (implement in `chunker.py`):

```python
def chunk_section(
    section_text: str,
    section_title: str,
    target_tokens: int = 800,
    overlap_tokens: int = 100,
) -> list[Chunk]:
    """
    Splits section text into overlapping chunks, respecting boundaries.
    
    Priority order for split points:
    1. Paragraph boundaries (double newline)
    2. Sentence boundaries (period + space/newline)
    3. Hard cut at target_tokens * 1.2 (last resort)
    """
    paragraphs = section_text.split("\n\n")
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    
    chunks = []
    current_text = ""
    
    for para in paragraphs:
        para_tokens = estimate_tokens(para)
        current_tokens = estimate_tokens(current_text)
        
        # Case 1: Adding this paragraph would exceed target
        if current_tokens + para_tokens > target_tokens and current_text:
            chunks.append(current_text.strip())
            # Start new chunk with overlap from end of previous
            overlap_text = get_last_n_tokens(current_text, overlap_tokens)
            current_text = overlap_text + "\n\n" + para
        
        # Case 2: Single paragraph exceeds target — split at sentences
        elif para_tokens > target_tokens:
            if current_text:
                chunks.append(current_text.strip())
            sentence_chunks = split_at_sentences(para, target_tokens, overlap_tokens)
            chunks.extend(sentence_chunks[:-1])
            current_text = sentence_chunks[-1] if sentence_chunks else ""
        
        # Case 3: Paragraph fits — accumulate
        else:
            if current_text:
                current_text += "\n\n" + para
            else:
                current_text = para
    
    if current_text.strip():
        chunks.append(current_text.strip())
    
    return [
        Chunk(
            text=chunk_text,
            section_title=section_title,
            chunk_index=i,
            token_count=estimate_tokens(chunk_text),
        )
        for i, chunk_text in enumerate(chunks)
    ]


def split_at_sentences(text: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a long paragraph at sentence boundaries."""
    # Regex for sentence endings: period/question/exclamation + space or newline
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    chunks = []
    current = ""
    for sent in sentences:
        if estimate_tokens(current + " " + sent) > target_tokens and current:
            chunks.append(current.strip())
            overlap_text = get_last_n_tokens(current, overlap_tokens)
            current = overlap_text + " " + sent
        else:
            current = (current + " " + sent).strip()
    if current.strip():
        chunks.append(current.strip())
    return chunks
```

**Table chunks**: Tables detected in Step 4c (>= 300 tokens) become
standalone chunks. Don't apply the overlap logic to them — they are atomic.
Set their `section_title` to the section they appeared in, and add a prefix
to the text: `"[Table] "`.

**What NOT to chunk**: Sections not in `NARRATIVE_SECTIONS` (like ITEM 8
Financial Statements) are not chunked. They only get section summaries
(Level 2).

**Output**: A list of `Chunk` objects per document, with continuous
`chunk_index` numbering (0, 1, 2, ...) across all sections. Record which
chunk_index range belongs to each section (needed for section_summaries
table).

---

### Step 6 — Embed chunks (→ Level 3 stored)

**What**: Generate a 384-dim vector for each chunk using
`all-MiniLM-L6-v2`, then insert into `document_chunks`.

**Model loading**: Load the model once at pipeline startup. Keep it in
memory across all documents. Do NOT reload per document.

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")
```

**Context-enriched embedding input**: The text stored in the `text` column
(what Claude reads) is clean. But the text you embed should include
context:

```python
def build_embedding_input(chunk: Chunk, company_name: str, doc_type: str) -> str:
    return f"{company_name}, {doc_type}, {chunk.section_title}: {chunk.text}"
```

Store BOTH in the database:
- `text` = clean chunk text (for Claude to read)
- `embedding_input` = context-enriched text (for debugging/reembedding)
- `embedding` = model.encode(embedding_input)

**Batch embedding**: Embed all chunks for a document in one batch:
```python
embedding_inputs = [build_embedding_input(c, company, doc_type) for c in chunks]
vectors = model.encode(embedding_inputs, batch_size=32, show_progress_bar=False)
```

**Insert**: Batch insert into `document_chunks`. Use executemany or COPY
for speed.

---

### Step 7 — Generate and embed section summaries (→ Level 2)

**What**: For each section that was chunked, generate a 2-3 sentence
summary and embed it.

**Phase 1 approach — extractive summary** (no LLM cost):
Take the first sentence of the section, plus the first sentence of every
3rd paragraph. Cap at 3 sentences total. This is crude but fast and free.

```python
def extractive_summary(section_text: str, max_sentences: int = 3) -> str:
    paragraphs = [p.strip() for p in section_text.split("\n\n") if p.strip()]
    sentences = []
    for i, para in enumerate(paragraphs):
        if i == 0 or i % 3 == 0:
            first_sentence = re.split(r'(?<=[.!?])\s+', para)[0]
            sentences.append(first_sentence)
        if len(sentences) >= max_sentences:
            break
    return " ".join(sentences)
```

**Phase 2 approach — LLM-generated summary** (better quality, costs ~$0.001 per section):
```python
response = anthropic_client.messages.create(
    model="claude-haiku-4-5-20241022",
    max_tokens=200,
    messages=[{
        "role": "user",
        "content": f"""Summarize this section from {company_name}'s {doc_type}
filing (period ending {period_end}) in exactly 2-3 sentences. Focus on the
key facts, risks, or changes mentioned. Be specific — include names,
numbers, and topics. Do not be vague or generic.

Section: {section_title}

Text (first 6000 chars):
{section_text[:6000]}"""
    }]
)
summary_text = response.content[0].text
```

**Start with extractive (Phase 1). Switch to LLM (Phase 2) when retrieval
quality is tested and you want better results.** Make this a config flag:
`summary_method: "extractive"` or `"llm"`.

**Embed the summary**: Same context-enrichment pattern:
```python
embed_input = f"{company_name}, {doc_type}, {period_end}, {section_title}: {summary_text}"
summary_vector = model.encode(embed_input)
```

**Also create summaries for NON-CHUNKED sections**: Sections like ITEM 8
(Financial Statements) don't get chunked, but they still get a section
summary. Use the extractive approach on the raw section text. This gives
Claude navigational awareness of all sections, even ones it can't drill
into with search_passages.

**Insert** into `section_summaries` with `chunk_start_index` and
`chunk_end_index` linking to the chunk range for this section (NULL for
non-chunked sections).

---

### Step 8 — Generate and embed document summary (→ Level 1)

**What**: Summarize the entire filing into one paragraph using the section
summaries as input.

**Implementation**:
```python
section_texts = "\n".join(
    f"- {s.section_title}: {s.summary_text}"
    for s in section_summaries
)

response = anthropic_client.messages.create(
    model="claude-haiku-4-5-20241022",
    max_tokens=300,
    messages=[{
        "role": "user",
        "content": f"""Here are section summaries from {company_name}'s
{doc_type} for the period ending {period_end}.

Write a single paragraph (3-4 sentences) capturing the overall picture:
the company's financial position, key themes, notable risks, and any
major changes. Be specific — mention numbers and topics.

{section_texts}

Also return a JSON array of 3-5 key theme tags (lowercase, underscored)
after your paragraph, on a new line prefixed with "THEMES:".
Example: THEMES: ["supply_chain_risk", "revenue_growth", "regulatory_pressure"]"""
    }]
)

# Parse summary text and themes from response
raw = response.content[0].text
if "THEMES:" in raw:
    summary_text = raw[:raw.index("THEMES:")].strip()
    themes_str = raw[raw.index("THEMES:") + 7:].strip()
    key_themes = json.loads(themes_str)
else:
    summary_text = raw.strip()
    key_themes = []
```

**Embed**:
```python
embed_input = f"{company_name}, {doc_type}, {period_end}: {summary_text}"
summary_vector = model.encode(embed_input)
```

**Insert** into `document_summaries` with `key_themes` array.

**Fallback if LLM call fails**: Concatenate the first sentence of each
section summary. Ugly but ensures the row exists.

---

### Step 9 — Update document status to 'indexed'

After all levels are populated for a document, update its status:
```sql
UPDATE documents SET status = 'indexed' WHERE document_id = %s;
```

This is the signal that the document is fully ingested and queryable.

---

### Pipeline orchestrator (pipeline.py)

The orchestrator runs all steps in order for one company:

```python
async def ingest_company(company: Company, since: date) -> IngestResult:
    """
    Full ingestion pipeline for one company.
    Steps are idempotent — safe to re-run.
    """
    result = IngestResult(company=company)
    
    # Level 4: Structured data
    xbrl_data = await fetcher_xbrl.fetch(company.cik)
    if not is_duplicate(xbrl_data):
        line_items = parser_xbrl.parse(xbrl_data, company, since)
        db.upsert_line_items(line_items)
        result.line_items_count = len(line_items)
    
    # Levels 1-3: Narrative data
    filings = await fetcher_filings.list_filings(company.cik, since)
    
    for filing in filings:
        html = await fetcher_filings.fetch_document(filing)
        if is_duplicate(html):
            continue
        
        doc = db.insert_document(company, filing, hash=sha256(html))
        
        # Step 4: Parse HTML → sections
        sections = parser_html.extract_sections(html)
        db.update_status(doc, 'parsed')
        
        # Step 5: Chunk narrative sections
        all_chunks = []
        section_chunk_ranges = {}
        for section in sections:
            if section.title in NARRATIVE_SECTIONS:
                chunks = chunker.chunk_section(section.text, section.title)
                start_idx = len(all_chunks)
                all_chunks.extend(chunks)
                end_idx = len(all_chunks) - 1
                section_chunk_ranges[section.title] = (start_idx, end_idx)
        
        # Step 6: Embed and store chunks
        vectors = embedder.embed_chunks(all_chunks, company.name, filing.doc_type)
        db.insert_chunks(doc, all_chunks, vectors)
        db.update_status(doc, 'normalized')
        
        # Step 7: Section summaries (for ALL sections, not just chunked ones)
        section_summaries = []
        for section in sections:
            summary = summarizer.summarize_section(section, company, filing)
            chunk_range = section_chunk_ranges.get(section.title)
            summary_vec = embedder.embed_summary(summary, company, filing, section.title)
            section_summaries.append((summary, summary_vec, chunk_range))
        db.insert_section_summaries(doc, section_summaries)
        
        # Step 8: Document summary (from section summaries)
        doc_summary = summarizer.summarize_document(section_summaries, company, filing)
        doc_summary_vec = embedder.embed_doc_summary(doc_summary, company, filing)
        db.insert_document_summary(doc, doc_summary, doc_summary_vec)
        
        # Step 9: Mark complete
        db.update_status(doc, 'indexed')
        result.documents_indexed += 1
    
    return result
```

**Error handling per document**: If any step fails for a document, log the
error with full traceback, leave the document at its last successful status,
and continue to the next document. Don't let one bad filing kill the entire
ingestion run.

---

### CLI entry point (cli.py)

```bash
# Ingest specific companies
python -m app.ingest --tickers AAPL,MSFT --since 2022-01-01

# Ingest all configured companies
python -m app.ingest --all --since 2023-01-01

# Ingest with LLM summaries instead of extractive
python -m app.ingest --tickers AAPL --since 2022-01-01 --summary-method llm

# Show ingestion status
python -m app.ingest --status
```

**No background scheduler.** 10-K/10-Q filings only appear ~4 times per
year per company. For the demo, run ingestion manually via CLI. In
production you'd add a daily cron job or subscribe to EDGAR's RSS feed
for new filing notifications — but that's out of scope here.

---

## Tool layer for Claude

Five tools, each as a narrow function with a JSON schema. Every tool
returns citation metadata (document_id, filed_at, period_end at minimum).
Cap all result sizes. Log every tool call (inputs, outputs, latency).

### Tool 1: search_companies
```
search_companies(query: str) -> list[CompanyResult]
```
Fuzzy match on name/ticker. Returns up to 5 candidates with company_id,
ticker, name. Use ILIKE or trigram similarity.

### Tool 2: get_document_overview (Level 1)
```
get_document_overview(
    company_id: int,
    doc_types: list[str] | None = None,
    limit: int = 5
) -> list[DocumentOverview]
```
Returns document-level summaries, most recent first. For broad questions
about a company's overall position. Each result includes: summary_text,
doc_type, period_end, filed_at, key_themes.

### Tool 3: search_sections (Level 2)
```
search_sections(
    company_id: int,
    query: str,
    doc_types: list[str] | None = None,
    limit: int = 5
) -> list[SectionResult]
```
Semantic search over section summaries. Returns: summary_text,
section_title, document_id, doc_type, period_end, filed_at, distance.
Claude uses this to decide WHICH sections to drill into.

### Tool 4: search_passages (Level 3)
```
search_passages(
    company_id: int,
    query: str,
    section_titles: list[str] | None = None,  # narrow to specific sections
    doc_types: list[str] | None = None,
    limit: int = 5,
    distance_threshold: float = 0.5
) -> list[PassageResult]
```
Semantic search over document chunks. If section_titles is provided, only
searches within those sections (SQL WHERE clause before vector ordering).
Returns: text, section_title, document_id, filed_at, period_end, distance.
If best distance > threshold, return with a warning flag so Claude knows
the evidence is weak.

### Tool 5: get_financial_line_items (Level 4)
```
get_financial_line_items(
    company_id: int,
    statement_type: str,     # 'income' | 'balance' | 'cashflow'
    period_type: str = 'FY', # 'FY' | 'Q'
    line_items: list[str] | None = None,  # filter to specific items
    limit: int = 8
) -> list[LineItemResult]
```
Returns exact financial figures, most recent periods first. Pure SQL, no
vector search. Each result includes: line_item, value, currency, unit,
period_end, period_type, document_id, filed_at.

---

## Claude API integration

### SDK and model
- Use the Anthropic Python SDK
- Chat model: `claude-sonnet-4-5-20241022` (or current Sonnet — check SDK)
- Ingestion summary model: `claude-haiku-4-5-20241022`

### Tool-use loop
Implement the standard tool_use loop:
1. Call `messages.create()` with system prompt, conversation history, and
   tool definitions
2. If response contains `tool_use` blocks, execute the tool functions
   against the DB
3. Send `tool_result` blocks back
4. Repeat until `stop_reason == "end_turn"`
5. Stream the final text response to the frontend

### System prompt
```
You are a financial analyst assistant. You have access to a database of
SEC filings (10-K and 10-Q) for select US public companies.

RULES:
- Answer ONLY from data returned by your tools. Never speculate about
  figures you haven't looked up.
- Always cite the source: include the filing type, period, and date.
- If the data isn't available in your tools, say so clearly.
- For broad/overview questions, start with get_document_overview.
- For topic-specific questions, use search_sections first to find the
  right section, then search_passages to get the detail.
- For exact numbers, use get_financial_line_items.
- You can chain multiple tool calls to answer complex questions.

AVAILABLE COMPANIES: {list_from_config}
```

### Prompt caching
Enable prompt caching on the system prompt and tool definitions. These
don't change between turns, so they should be cached.

### Conversation history management
Keep full tool results for the last 3 turns. For older turns, replace
tool results with a brief summary to prevent context window bloat.
Implement this as a simple history truncation function.

---

## Frontend

Minimal chat interface:
- Message input + send button
- Streaming assistant responses (SSE from backend)
- User messages and assistant messages in a scrollable thread
- Citations rendered as clickable/hoverable references showing:
  filing type, company, period, filed date
- A "data status" indicator showing when data was last ingested
- No auth required

---

## Configuration

### Environment variables (.env.example)
```
ANTHROPIC_API_KEY=sk-ant-...
SEC_USER_AGENT_EMAIL=your_email@example.com
DATABASE_URL=postgresql://finagent:password@postgres:5432/finagent
READONLY_DATABASE_URL=postgresql://readonly:password@postgres:5432/finagent
```

### Config file (config.yaml)
```yaml
companies:
  - ticker: AAPL
    name: Apple Inc.
    cik: "0000320193"
  - ticker: MSFT
    name: Microsoft Corporation
    cik: "0000789019"
  - ticker: GOOGL
    name: Alphabet Inc.
    cik: "0001652044"

ingestion:
  since: "2022-01-01"
  schedule_interval_hours: 1
  embedding_model: "all-MiniLM-L6-v2"

chunking:
  target_tokens: 800
  overlap_tokens: 100

retrieval:
  distance_threshold: 0.5
  max_results_per_tool: 5
```

### Docker Compose
Services: postgres (with pgvector), backend (FastAPI + chat), ingestion
(worker + scheduler), frontend. Use a shared `.env` file.

### README.md
Include:
- Architecture diagram (ASCII is fine)
- Prerequisites (Docker, API key)
- Setup steps (`cp .env.example .env`, edit, `docker compose up`)
- How to run ingestion manually
- How to use the chat
- How to add more companies
- How the four-level retrieval works (brief explanation)

---

## How I want you to work

### Phase 0 — Plan
Before writing any code, produce:
- Directory layout (tree format) — must include `app/ingestion/` module
  structure as specified above
- Exact Python and JS dependencies with versions
- Build order (what you'll do in each phase)
- Any decisions that differ from this brief and why

**Stop and wait for my confirmation before proceeding.**

### Phase 1 — Skeleton
- docker-compose.yml with services: postgres (with pgvector), backend
  (FastAPI — serves both chat and ingestion CLI), frontend
- DB schema via Alembic migrations — ALL tables including document_chunks,
  section_summaries, document_summaries (even though they're populated later)
- Empty FastAPI app with health check (`/health`)
- DB user setup: `finagent` (read-write for ingestion), `readonly` (SELECT
  only, for chat backend). Put user creation in a DB init SQL script that
  docker-compose runs on first startup.
- config.yaml with the 3 starter companies and their CIKs
- Verify: `docker compose up` works. Connect to DB, confirm all 6 tables
  exist. Confirm readonly user can SELECT but cannot INSERT.

**Stop. Tell me what to verify and what's next.**

### Phase 2 — Ingestion: EDGAR client + XBRL structured data (Steps 1-2)
- `edgar_client.py` with rate limiter (token bucket, 10 req/s) and
  User-Agent header
- `fetcher_xbrl.py` — fetch companyfacts JSON for a company
- `parser_xbrl.py` — parse JSON, filter by taxonomy mapping, filter by
  date, handle duplicate concepts and amended filings
- Include the full XBRL_TAXONOMY dict (~40 concepts) and STATEMENT_TYPES
  mapping as specified in the ingestion spec
- Dedup by raw_hash on documents table, upsert with ON CONFLICT DO NOTHING
  on financial_line_items
- CLI: `python -m app.ingest --tickers AAPL --since 2022-01-01`
- Verify: run against AAPL. Show me:
  1. Row count in financial_line_items
  2. Sample query: `SELECT line_item, period_end, period_type, value FROM
     financial_line_items WHERE company_id=1 AND line_item='revenue'
     ORDER BY period_end DESC LIMIT 5;`
  3. Confirm running the same command again inserts 0 new rows (idempotency)

**Stop. Tell me what to verify and what's next.**

### Phase 3a — Ingestion: Filing HTML fetch + section extraction (Steps 3-4)
- `fetcher_filings.py` — query EDGAR submissions API to list filings,
  construct filing URLs, download HTML
- `parser_html.py` — strip boilerplate (Step 4a), detect section boundaries
  with SECTION_PATTERNS regex (Step 4b), handle tables (Step 4c)
- Include the full SECTION_PATTERNS list and NARRATIVE_SECTIONS config set
  as specified
- Dedup by sha256 of HTML bytes
- Handle edge cases: table of contents detection, missing sections (fall
  back to "UNSTRUCTURED"), non-HTML filings (skip with warning)
- Store parsed document in documents table with status='parsed'
- Verify: run against AAPL's most recent 10-K. Show me:
  1. List of detected sections with titles and approximate text length
  2. A 200-char sample from the ITEM 1A. RISK FACTORS section (confirm
     text is clean — no HTML tags, no XBRL tags, no page headers)
  3. Confirm table of contents was skipped

**Stop. Tell me what to verify and what's next.**

### Phase 3b — Ingestion: Chunking + embedding (Steps 5-6)
- `chunker.py` — implement chunk_section() with paragraph-boundary-aware
  splitting, sentence fallback, overlap, and table chunk handling exactly
  as specified
- `embedder.py` — load MiniLM-L6-v2 once, implement context-enriched
  embedding input (company + doc_type + section_title prefix), batch encode
- Store chunks in document_chunks with both `text` (clean) and
  `embedding_input` (enriched) columns
- Update document status to 'normalized'
- Verify: run full ingest for AAPL (XBRL + one 10-K). Show me:
  1. Total chunks created for the 10-K
  2. Chunk size distribution (min, max, avg token count)
  3. A sample chunk from ITEM 1A with its section_title
  4. Confirm embedding dimension is 384
  5. A sample vector search:
     `SELECT text, section_title, embedding <=> '[query_vector]'::vector AS dist
      FROM document_chunks ORDER BY dist LIMIT 3;`
     (you can hardcode a test query vector for verification)

**Stop. Tell me what to verify and what's next.**

### Phase 3c — Ingestion: Summaries (Steps 7-8)
- `summarizer.py` — implement extractive_summary() for section summaries
  (Phase 1 approach). Implement LLM-generated document summary using Claude
  Haiku (with key_themes extraction)
- Generate section summaries for ALL sections (including non-chunked ones)
- Generate document summary from section summaries
- Embed all summaries with context-enriched input
- Update document status to 'indexed'
- Config flag `summary_method: "extractive"` for sections (default),
  `"llm"` as upgrade option
- Verify: run full pipeline for AAPL. Show me:
  1. All section summaries for the most recent 10-K (title + summary_text)
  2. The document summary for that 10-K
  3. The key_themes array
  4. Document status is 'indexed'

**Stop. Tell me what to verify and what's next.**

### Phase 4 — Tools + Claude loop (CLI chat)
- Implement all 5 tools with Pydantic input/output models
- Each tool connects to DB via readonly user
- search_passages supports section_titles filter (WHERE clause before
  vector ordering) and distance_threshold
- Tool-use loop with retry and backoff on Anthropic API calls
- System prompt with the multi-level retrieval guidance as specified
- CLI chat: `python -m app.chat`
- Test with these three queries (they exercise different levels):
  1. "What was Apple's revenue last year?" → should call
     get_financial_line_items (Level 4)
  2. "What supply chain risks did Apple mention?" → should call
     search_sections (Level 2) then search_passages (Level 3)
  3. "Give me an overview of Apple's financial health" → should call
     get_document_overview (Level 1)
- Verify: all three queries return reasonable answers with citations.
  Show me the tool call sequence for each query.

**Stop. Tell me what to verify and what's next.**

### Phase 5 — Web UI
- FastAPI SSE endpoint for streaming chat responses
- Minimal frontend (React/Vite or plain HTML) with:
  - Chat message thread (user + assistant messages)
  - Streaming text display
  - Citation markers (inline) that expand on click/hover to show:
    filing type, company, period, section, filed date
- Data status indicator (last ingestion time)
- Verify: open browser at localhost:3000 (or wherever), ask "What risks
  did Apple flag in their 2024 10-K?", see streaming response with
  clickable citations.

**Stop. Tell me what to verify and what's next.**

### Phase 6 — Polish
- Tool call logging: log every call with tool_name, inputs, output
  row count, latency_ms (use structured JSON logging)
- Conversation history management: keep full tool results for last 3 turns,
  summarize older ones
- Data status endpoint: `GET /api/status` → last ingestion time per company,
  document count, chunk count
- README.md with architecture, setup, usage
- `make smoke` target: ingest AAPL since 2024-01-01 + run 3 test chat
  queries + verify non-empty responses

**Done. Final review.**

---

## Code standards

- Small files. No file over 300 lines. Split into modules.
- Type hints on all Python. Pydantic models for tool inputs/outputs.
- Every external call (EDGAR, Claude API) has retry with exponential
  backoff (3 attempts, 1s/2s/4s).
- Use `logging` module, not print(). Structured log format.
- Tests are not required for the demo, but the `make smoke` target must
  work end-to-end.
- If you hit a real blocker (EDGAR rate limiting, XBRL parsing edge case,
  pgvector issue), stop and ask. Don't hack around it silently.

---

## Common pitfalls to avoid

- EDGAR requires a real email in User-Agent. Without it you get 403.
- EDGAR rate limit is 10 req/sec. Respect it or get blocked.
- The companyfacts JSON can be large (10MB+). Parse incrementally.
- MiniLM-L6-v2 outputs 384 dimensions, NOT 1536. The schema must match.
- pgvector ivfflat index needs `lists` ≥ sqrt(row_count). For small
  datasets (<1000 rows) you can skip the index entirely — brute force
  is fast enough.
- Short texts (summaries) produce worse embeddings than long texts
  (chunks). The context-enriched embedding input compensates for this.
- Don't chunk tables — if you detect a <table> tag in the HTML, extract
  it as a single chunk regardless of size, or skip it (the structured
  data in Level 4 covers most tables).
- The readonly DB user must be created BEFORE the chat backend starts.
  Put it in the DB init script.

Start with Phase 0. Show me the plan.
