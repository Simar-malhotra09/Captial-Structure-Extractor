# Capital Structure Extractor

Extracts a company's capital structure from SEC 10-K filing inputs (balance sheet, debt note, lease note) and renders a formatted HTML table with dependency graph.

## Architecture

- **Backend**: Python/FastAPI — parses iXBRL HTML, extracts instruments programmatically, validates with Claude LLM
- **Frontend**: Next.js/TypeScript/Tailwind — file upload, results table, dependency graph, source HTML viewer, corrections chat

### Pipeline

1. **NER**: Extract entity names (parent + subsidiaries) from debt note
2. **Table Parsing**: Walk HTML tables top-to-bottom, detect section headers for entity/priority assignment, extract amounts from `ix:nonfraction` tags (preferring net/carrying amounts over face/principal)
3. **Lease Parsing**: Extract finance + operating lease liabilities from lease note, deduplicate against debt table
4. **Balance Sheet**: Extract cash & NCI for Net Debt and Enterprise Value calculation
5. **LLM Validation**: Claude reviews programmatic extraction, corrects entity assignments, resolves duplicates, adds issue dates and available capacity from narrative
6. **Assembly**: Sort by issuer (subsidiaries first) → priority → instrument type, compute totals

### Key Features

- **Three-tab output**: Capital Structure table, Dependency Graph (mermaid.js), Source HTML with cross-referencing
- **Corrections Chat**: Real-time LLM-powered corrections — add/remove/modify instruments without re-running extraction
- **Confidence Scores**: LLM rates each correction 0-100%
- **Source Citations**: Click any instrument's "Source" column to jump to the original filing table row
- **Duplicate Detection**: Flags identical amounts and sum matches across debt + lease sheets

## Setup

### Backend

```bash
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python server.py
# Runs on http://localhost:8000
```

### Frontend

```bash
cd frontend
npm install
echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > .env.local
npm run dev
# Runs on http://localhost:3000
```

### CLI (no web server needed)

```bash
cd backend
export ANTHROPIC_API_KEY=sk-ant-...
python graph.py path/to/company_dir -o output.html
# Expects: debt_note.html, lease_note.html, balance_sheet.json, metadata.json in the directory
```

## Deployment

### Backend (Railway)

1. Create new Railway project
2. Connect to GitHub repo, set root directory to `backend/`
3. Add environment variable: `ANTHROPIC_API_KEY=sk-ant-...`
4. Railway auto-detects the Dockerfile or Procfile
5. Note the deployed URL (e.g. `https://your-app.up.railway.app`)

### Frontend (Vercel)

1. Import repo to Vercel
2. Set root directory to `frontend/`
3. Add environment variable: `NEXT_PUBLIC_API_URL=https://your-railway-url.up.railway.app`
4. Deploy

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/extract` | Upload files, start extraction job |
| GET | `/api/jobs/{id}` | Poll job status + results |
| GET | `/api/jobs/{id}/prompt` | View LLM approach + corrections |
| GET | `/api/jobs/{id}/source` | View source tables (debt + lease) |
| POST | `/api/jobs/{id}/chat` | Send correction request |
| GET | `/api/health` | Health check |

## File Structure

```
backend/
  server.py       # FastAPI endpoints
  graph.py        # Core extraction pipeline
  ner.py          # Entity extraction (NER)
  requirements.txt
  Dockerfile
  Procfile

frontend/
  app/
    page.tsx      # Main UI (upload, table, graph, source, chat)
    layout.tsx
    globals.css
  lib/
    api.ts        # API client
  package.json
  next.config.js
```

## Evaluation Notes

- **Correctness**: Programmatic iXBRL parsing ensures amounts are deterministic. LLM handles classification (entity, priority) with confidence scores.
- **Consistency**: `temperature=0` on all LLM calls. Deterministic parsing layer means the same filing always produces the same raw extraction.
- **Citations**: Every instrument has a `source` field (e.g. "debt_note table 0 row 5") that cross-references to the Source HTML tab.
- **Self-assessment**: Confidence scores per correction, duplicate flags, approach summary. Corrections Chat allows user to verify and fix.
