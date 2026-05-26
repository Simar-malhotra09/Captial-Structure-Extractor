# Capital Structure Extractor



Upload a company's balance sheet (JSON), debt footnote (HTML), and lease footnote (HTML) from SEC 10-K filings. The app extracts the capital structure and renders it as a formatted table.

## Setup

### Backend

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY
uvicorn server:app --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > .env.local
npm run dev
```

### CLI (no web server)

```bash
export ANTHROPIC_API_KEY
python graph.py path/to/company_dir/ -o output.html
# Directory should contain: debt_note.html, lease_note.html, balance_sheet.json, metadata.json
```

## How It Works

The pipeline has two layers: deterministic parsing followed by LLM validation.

**Layer 1 — Programmatic extraction** (deterministic, same output every run):
- Parse `ix:nonfraction` tags from iXBRL HTML to get amounts, concepts, and context refs
- Walk tables top-to-bottom tracking section headers for entity/priority assignment
- Prefer net/carrying amounts (`LongTermDebt`, `SeniorNotes`) over face/principal (`DebtInstrumentFaceAmount`)
- Use the target period from `metadata.json` — first column in multi-year tables
- Extract finance + operating leases from the lease note, deduplicate against debt table
- Extract cash and NCI from balance sheet JSON
- Resolve footnote references (e.g. "(1)" → footnote text) and attach to each row
- Flag duplicate amounts and sum-matches across debt and lease sheets

**Layer 2 — LLM validation** (Claude Sonnet, temperature=0 for consistency):
- Entity/issuer assignment: which subsidiary issued each instrument, based on labels and narrative context
- Priority classification: Senior Secured vs Unsecured vs Guaranteed, based on instrument name (not guarantor language)
- Duplicate resolution: using footnotes and amount flags to decide what's double-counted
- Supplementary data: issue dates, available capacity, coupon rates, maturity years from the narrative
- Each correction has a confidence score (0-100%) and a reason explaining the decision

## Features

- **Three-tab output**: Capital Structure table, Dependency Graph (mermaid.js), Source HTML viewer
- **Source cross-referencing**: click the Source column on any row to jump to the original table/row in the filing
- **Corrections chat**: real-time LLM-powered corrections — type "move the 5.75% notes to Bausch + Lomb" and the table updates live
- **Progress bar**: real percentage with time estimate and step-by-step status (not just a spinner)
- **Subtotals**: per priority tier, plus Total Debt → Cash → Net Debt → NCI → Market Cap → Enterprise Value

## Design Decisions

**Why two layers instead of pure LLM?** The programmatic layer ensures amounts are always correct — they come directly from XBRL tags, not LLM extraction. The LLM handles classification (entity, priority) where the filing structure varies too much for rules. This means: if the LLM makes a wrong classification, the amounts are still right and the user can fix it via chat.

**Why not fine-tune or few-shot with training data?** The train set is too small (4-6 companies) and SEC filing structures vary wildly between companies. A prompt-based approach generalizes better to unseen companies than pattern-matching on training examples.

**Why temperature=0?** Consistency. The same filing should produce the same output on every run. The LLM layer is the only source of non-determinism, and temperature=0 minimizes that.

**Why a corrections chat?** SEC filings are inconsistent enough that no extraction pipeline will be 100% correct on every company. The chat lets users fix edge cases without re-running the full pipeline — it sends the current state + user request to Claude, applies corrections, recalculates totals, and re-renders immediately.

## Known Limitations

- Operating leases are sometimes excluded by the LLM when the debt table's "Other" row mentions lease liabilities (appears as double-counting). The chat can add them back.
- Entity assignment for companies with unusual abbreviations (e.g. "B+L" for "Bausch + Lomb") relies on the LLM recognizing the mapping from narrative context.
- Market cap is user-provided, not sourced programmatically.
- Some filings with deeply nested or non-standard iXBRL structures may not parse all instruments on the first pass.

## File Structure

```
server.py          FastAPI backend (endpoints, job management, chat)
graph.py           Core extraction pipeline (parsing, LLM validation, rendering)
ner.py             Entity extraction from narrative text
requirements.txt   Python dependencies
railway.json       Railway deployment config
frontend/
  app/page.tsx     Main UI (upload, table, graph, source, chat)
  lib/api.ts       API client with types
```
