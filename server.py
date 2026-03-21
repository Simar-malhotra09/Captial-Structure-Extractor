import os
import uuid
import json
import asyncio
import sys
from io import StringIO
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from graph import (
    parse_instruments,
    parse_leases,
    parse_balance_sheet,
    deduplicate_leases,
    llm_validate,
    apply_corrections,
    build_mermaid,
    html_to_text,
)
from ner import extract_entities

app = FastAPI(title="Capital Structure Extractor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=4)

# In-memory stores
jobs = {}
prompts = {}


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "active": sum(1 for j in jobs.values() if j["status"] == "running"),
    }


@app.post("/api/extract")
async def start_extraction(
    debt_note: UploadFile = File(...),
    lease_note: UploadFile = File(...),
    balance_sheet: UploadFile = File(...),
    metadata: UploadFile = File(None),
    market_cap: float = Form(0),
):
    debt_html = (await debt_note.read()).decode("utf-8")
    lease_html = (await lease_note.read()).decode("utf-8")

    try:
        bs_json = json.loads(await balance_sheet.read())
    except Exception as e:
        raise HTTPException(400, f"Invalid balance_sheet.json: {e}")

    annual_period = 2024
    if metadata:
        try:
            meta = json.loads(await metadata.read())
            annual_period = meta.get("annual_period", 2024)
        except:
            pass

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "status": "pending",
        "stage": "",
        "detail": "",
        "progress_pct": 0,
        "elapsed_sec": 0,
        "created_at": datetime.now().isoformat(),
        "result": None,
        "error": None,
        "debt_html": debt_html,
        "lease_html": lease_html,
    }

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        executor,
        _run_job,
        job_id,
        debt_html,
        lease_html,
        bs_json,
        annual_period,
        market_cap,
        api_key,
    )

    return {"job_id": job_id, "status": "pending"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    # Return without the stored HTML (too large for polling)
    j = {k: v for k, v in jobs[job_id].items() if k not in ("debt_html", "lease_html")}
    return j


@app.get("/api/jobs/{job_id}/prompt")
def get_prompt(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return PlainTextResponse(prompts.get(job_id, "(not available yet)"))


@app.get("/api/jobs/{job_id}/source")
def get_source(job_id: str):
    """Return only the tables from debt + lease HTML, with anchor IDs for cross-referencing."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    from bs4 import BeautifulSoup

    parts = []
    for key, label in [("debt_html", "Debt Note"), ("lease_html", "Lease Note")]:
        raw = jobs[job_id].get(key, "")
        if not raw:
            continue
        soup = BeautifulSoup(raw, "html.parser")
        tables = soup.find_all("table")
        if tables:
            parts.append(
                f'<h3 style="font-family:sans-serif;font-size:14px;margin:16px 0 8px;color:#333">{label} ({len(tables)} tables)</h3>'
            )
            for tidx, table in enumerate(tables):
                # Add anchor ID matching our source refs
                prefix = "debt_note" if "debt" in key else "lease_note"
                table["id"] = f"source-{prefix}-table-{tidx}"
                table["style"] = (
                    table.get("style", "")
                    + ";border:1px solid #ddd;margin-bottom:12px;"
                )
                # Add row IDs
                for ridx, tr in enumerate(table.find_all("tr")):
                    tr["id"] = f"source-{prefix}-t{tidx}-r{ridx}"
                parts.append(str(table))

    html = (
        '<div style="font-family:Times New Roman,serif;font-size:10pt;padding:8px">'
        + "\n".join(parts)
        + "</div>"
    )
    return PlainTextResponse(html, media_type="text/html")


CHAT_SYSTEM = """You are reviewing a capital structure extraction. The user sees the current output table and wants to make corrections.

You receive: the current instruments list and the user's message.

Return ONLY valid JSON with corrections in the same format:
{
  "corrections": [
    {
      "id": "instrument id to modify",
      "entity": "new entity or null",
      "priority": "new priority or null",
      "exclude": true/false,
      "amount_mm": new amount or null,
      "amount_available_mm": new amount or null,
      "clean_name": "new name or null",
      "reason": "what the user asked to change"
    }
  ],
  "message": "brief confirmation of what you changed"
}

If the user asks to ADD an instrument that's missing, use id "new_N" (e.g. "new_1").
For new instruments, include all fields: id, entity, priority, amount_mm, clean_name, coupon, maturity_year, etc.
If you can't fulfill the request, return {"corrections": [], "message": "explanation of why"}."""


from pydantic import BaseModel as PydanticModel


class ChatRequest(PydanticModel):
    message: str


@app.post("/api/jobs/{job_id}/chat")
async def chat_correction(job_id: str, req: ChatRequest):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    job = jobs[job_id]
    if job["status"] != "done" or not job.get("result"):
        raise HTTPException(400, "Job not complete")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "No API key configured")

    import httpx

    # Send current instruments + user message
    current = job["result"]["instruments"]
    compact = [
        {
            "id": i["id"],
            "label": i.get("clean_name") or i["label"],
            "amount_mm": i.get("amount_mm"),
            "amount_available_mm": i.get("amount_available_mm"),
            "priority": i.get("priority"),
            "entity": i.get("entity"),
            "coupon": i.get("coupon") or i.get("rate"),
            "maturity_year": i.get("maturity_year"),
        }
        for i in current
    ]

    user_msg = json.dumps(
        {
            "current_instruments": compact,
            "excluded": [
                {
                    "id": i["id"],
                    "label": i["label"],
                    "amount_mm": i.get("amount_mm"),
                    "reason": i.get("_reason", ""),
                }
                for i in job["result"].get("excluded", [])
            ],
            "user_request": req.message,
        },
        indent=2,
        default=str,
    )

    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "temperature": 0,
            "system": CHAT_SYSTEM,
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()

    # Parse response
    try:
        result = json.loads(text)
    except:
        fb = text.find("{")
        lb = text.rfind("}")
        if fb != -1 and lb > fb:
            result = json.loads(text[fb : lb + 1])
        else:
            return {"corrections": [], "message": "Failed to parse LLM response"}

    # Apply corrections to the job result
    corrections = result.get("corrections", [])
    if corrections:
        inst_by_id = {i["id"]: i for i in job["result"]["instruments"]}

        for corr in corrections:
            cid = corr["id"]

            if cid.startswith("new_"):
                # Add new instrument
                new_inst = {
                    "id": cid,
                    "label": corr.get("clean_name", "New instrument"),
                    "clean_name": corr.get("clean_name"),
                    "amount_mm": corr.get("amount_mm"),
                    "amount_available_mm": corr.get("amount_available_mm"),
                    "priority": corr.get("priority", "unknown"),
                    "entity": corr.get("entity", job["result"]["company_name"]),
                    "type": "unknown",
                    "rate": corr.get("coupon"),
                    "coupon": corr.get("coupon"),
                    "maturity_year": corr.get("maturity_year"),
                    "source": "user chat",
                    "_reason": corr.get("reason", "Added by user"),
                    "_confidence": 1.0,
                }
                job["result"]["instruments"].append(new_inst)
            elif cid in inst_by_id:
                inst = inst_by_id[cid]
                if corr.get("exclude"):
                    # Move to excluded
                    job["result"]["instruments"] = [
                        i for i in job["result"]["instruments"] if i["id"] != cid
                    ]
                    inst["_excluded"] = True
                    inst["_reason"] = corr.get("reason", "Removed by user")
                    job["result"]["excluded"].append(inst)
                else:
                    if corr.get("entity"):
                        inst["entity"] = corr["entity"]
                    if corr.get("priority"):
                        inst["priority"] = corr["priority"]
                    if corr.get("amount_mm") is not None:
                        inst["amount_mm"] = corr["amount_mm"]
                    if corr.get("amount_available_mm") is not None:
                        inst["amount_available_mm"] = corr["amount_available_mm"]
                    if corr.get("clean_name"):
                        inst["clean_name"] = corr["clean_name"]
                    inst["_reason"] = corr.get("reason", "Modified by user")
                    inst["_confidence"] = 1.0
            else:
                # Check if it's in excluded — user wants to un-exclude
                excl_by_id = {i["id"]: i for i in job["result"].get("excluded", [])}
                if cid in excl_by_id:
                    inst = excl_by_id[cid]
                    inst.pop("_excluded", None)
                    inst["_reason"] = corr.get("reason", "Re-included by user")
                    inst["_confidence"] = 1.0
                    if corr.get("entity"):
                        inst["entity"] = corr["entity"]
                    if corr.get("priority"):
                        inst["priority"] = corr["priority"]
                    # Move from excluded to active
                    job["result"]["excluded"] = [
                        i for i in job["result"]["excluded"] if i["id"] != cid
                    ]
                    job["result"]["instruments"].append(inst)

        # Rebuild entity_instruments map from scratch (most reliable)
        entity_instruments = {}
        for inst in job["result"]["instruments"]:
            ent = inst.get("entity")
            if ent:
                if ent not in entity_instruments:
                    entity_instruments[ent] = []
                entity_instruments[ent].append(inst["id"])
        job["result"]["entity_instruments"] = entity_instruments

        # Rebuild entities list to match what's actually in use
        # Preserve original order but add any new entities
        old_entities = job["result"].get("entities", [])
        new_entities = [e for e in entity_instruments.keys() if e not in old_entities]
        job["result"]["entities"] = [
            e for e in old_entities if e in entity_instruments
        ] + new_entities

        # Recalculate totals
        active = job["result"]["instruments"]
        total_debt = sum(i.get("amount_mm", 0) or 0 for i in active)
        cash = job["result"]["cash_mm"]
        nci = job["result"]["nci_mm"]
        mcap = job["result"]["market_cap_mm"]
        job["result"]["total_debt_mm"] = round(total_debt, 3)
        job["result"]["net_debt_mm"] = round(total_debt - cash, 3)
        job["result"]["enterprise_value_mm"] = round(total_debt - cash + nci + mcap, 3)

    return {
        "corrections_applied": len(corrections),
        "message": result.get("message", ""),
        "result": job["result"],
    }


def _run_job(
    job_id, debt_html, lease_html, bs_json, annual_period, market_cap, api_key
):
    job = jobs[job_id]
    job["status"] = "running"

    # Capture stderr for debugging
    log = StringIO()
    old_stderr = sys.stderr

    try:
        import time

        start_time = time.time()

        def progress(stage, detail="", pct=0):
            job["stage"] = stage
            job["detail"] = detail
            job["progress_pct"] = pct
            job["elapsed_sec"] = round(time.time() - start_time, 1)

        # Step 1/6: Parse text
        progress("Extracting text", "Reading debt note and extracting plain text...", 5)
        text = html_to_text(debt_html)

        # Step 2/6: NER
        if api_key:
            progress(
                "Identifying entities",
                f"Calling LLM to find companies in {len(text)} chars of text...",
                15,
            )
            entities = extract_entities(text, api_key)
        else:
            entities = []

        entities = sorted(entities, key=len, reverse=True)
        filtered = []
        for e in entities:
            if not any(e.lower() in kept.lower() for kept in filtered):
                filtered.append(e)
        entities = filtered

        parent = None
        for e in entities:
            if any(
                s in e.lower() for s in ("inc", "corporation", "corp", "company", "co.")
            ):
                parent = e
                break
        if not parent and entities:
            parent = entities[0]

        # Step 3/6: Parse instruments
        progress(
            "Parsing debt table",
            f"Found {len(entities)} entities, extracting instruments from iXBRL...",
            30,
        )
        instruments = parse_instruments(debt_html, entities)

        # Step 4/6: Parse leases
        progress(
            "Parsing lease note",
            f"{len(instruments)} debt rows found, now processing leases...",
            45,
        )
        lease_instruments = parse_leases(lease_html, parent or "Unknown")
        new_leases = deduplicate_leases(instruments, lease_instruments)
        progress(
            "Parsing lease note",
            f"{len(lease_instruments)} lease items found, {len(new_leases)} new after dedup",
            50,
        )
        instruments.extend(new_leases)

        # Step 5/6: Balance sheet
        progress("Parsing balance sheet", "Extracting cash and NCI...", 55)
        bs_data = parse_balance_sheet(bs_json)

        # Step 6/6: LLM validation (longest step)
        llm_corrections = None
        if api_key:
            n = len(instruments)
            progress(
                "LLM validation",
                f"Sending {n} rows to Claude for entity assignment, priority classification, and duplicate resolution. This is the slowest step (~30-60s)...",
                60,
            )
            llm_corrections = llm_validate(
                instruments, entities, text, api_key, annual_period
            )
            progress(
                "LLM validation",
                f"Received {len(llm_corrections.get('corrections', []))} corrections, applying...",
                90,
            )
            instruments = apply_corrections(instruments, llm_corrections)
            prompts[job_id] = json.dumps(llm_corrections, indent=2, default=str)

        # Assembly
        progress("Assembling output", "Computing totals and building graph...", 95)
        active = [i for i in instruments if not i.get("_excluded")]
        excluded = [i for i in instruments if i.get("_excluded")]

        # Build entity map
        entity_instruments = {e: [] for e in entities}
        for inst in active:
            e = inst.get("entity")
            if e and e in entity_instruments:
                entity_instruments[e].append(inst["id"])

        # Build mermaid
        mermaid = build_mermaid(entities, active, entity_instruments)

        # Compute totals
        total_debt = sum(i.get("amount_mm", 0) or 0 for i in active)
        cash = bs_data.get("cash_mm", 0)
        nci = bs_data.get("nci_mm", 0)
        net_debt = round(total_debt - cash, 3)
        ev = round(net_debt + nci + market_cap, 3)

        job["result"] = {
            "company_name": llm_corrections.get("company_name", parent or "Unknown")
            if llm_corrections
            else (parent or "Unknown"),
            "approach": llm_corrections.get("approach", "") if llm_corrections else "",
            "entities": entities,
            "instruments": active,
            "excluded": excluded,
            "entity_instruments": {k: v for k, v in entity_instruments.items()},
            "mermaid": mermaid,
            "guarantor_relationships": llm_corrections.get(
                "guarantor_relationships", []
            )
            if llm_corrections
            else [],
            "total_debt_mm": round(total_debt, 3),
            "cash_mm": cash,
            "nci_mm": nci,
            "net_debt_mm": net_debt,
            "market_cap_mm": market_cap,
            "enterprise_value_mm": ev,
            "annual_period": annual_period,
            "lease_count": len(new_leases),
        }

        job["status"] = "done"
        job["stage"] = "Complete"
        job["detail"] = f"Net Debt: ${net_debt:,.1f}mm | {len(active)} instruments"
        job["progress_pct"] = 100

    except Exception as e:
        import traceback

        job["status"] = "error"
        job["stage"] = "error"
        job["error"] = str(e)
        job["detail"] = traceback.format_exc()[-500:]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
