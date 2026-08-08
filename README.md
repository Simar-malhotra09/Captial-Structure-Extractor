# Capital Structure Extractor

A tool for extracting a company's capital structure from SEC 10-K filings, with a human-in-the-loop workflow for reviewing and correcting the results.

Upload a company's balance sheet, debt footnote, and lease footnote. The app extracts the initial capital structure, shows the source for each value, and lets the user correct mistakes directly through a chat interface.

## Workflow

The extraction is intentionally split into an initial pass and an iterative review process.

The first pass uses XBRL data and document structure to extract amounts and identify potential debt and lease instruments. An LLM then helps classify ambiguous fields such as issuer, priority, and duplicate instruments.

The resulting table is presented alongside the original filing. Users can inspect individual rows, follow them back to their source, and correct anything that looks wrong. Corrections are made against the current table rather than rerunning the entire extraction pipeline. For example:

> "Move the 5.75% notes to Bausch + Lomb."

The system applies the change, recalculates the relevant totals, and keeps the source information attached to the row.

This makes the extraction process iterative: the model produces a first draft, the user reviews it, and the model handles targeted corrections instead of trying to get everything right in one pass.

## Features

* Capital structure table with debt, leases, cash, NCI, and enterprise value
* Source links for tracing each row back to the SEC filing
* Natural-language corrections through a chat interface
* Automatic recalculation after corrections
* Dependency graph showing relationships between entities and instruments
* Original filing viewer for reviewing extraction decisions
* Progress tracking for the extraction pipeline

## Extraction

The initial extraction combines deterministic parsing with LLM-based classification.

The parser handles values that can be reliably recovered from the filing, including:

* XBRL amounts and concepts
* Reporting periods
* Debt and lease tables
* Cash and NCI
* Footnote references
* Potential duplicates across tables

The LLM is used where interpretation is required, such as:

* Assigning instruments to the correct issuer
* Classifying debt priority
* Resolving possible duplicates
* Pulling supplementary information from narrative text

The goal is to keep the LLM involved where judgment is useful while making it easy for a user to catch and correct mistakes.

## CLI

The same pipeline can be run without the web application:

```bash
export ANTHROPIC_API_KEY
python graph.py path/to/company_dir/ -o output.html
```

The directory should contain:

```text
debt_note.html
lease_note.html
balance_sheet.json
metadata.json
```

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

## Limitations

SEC filings vary considerably, so some cases still require manual correction. In particular, unusual issuer abbreviations, non-standard iXBRL structures, and ambiguous lease/debt disclosures can lead to incorrect classifications or missed instruments.

Rather than trying to eliminate every edge case in the initial extraction, the workflow is designed around making these cases quick to identify and correct.

