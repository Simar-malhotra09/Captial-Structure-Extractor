"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import {
  submitExtraction,
  pollUntilDone,
  fetchSourceHtml,
  sendChatCorrection,
  type JobStatus,
  type ExtractionResult,
  type Instrument,
} from "@/lib/api";

type Stage = "idle" | "uploading" | "processing" | "done" | "error";
type View = "table" | "graph" | "source";

export default function Home() {
  const [stage, setStage] = useState<Stage>("idle");
  const [progress, setProgress] = useState("");
  const [result, setResult] = useState<ExtractionResult | null>(null);
  const [error, setError] = useState("");
  const [marketCap, setMarketCap] = useState("0");
  const [view, setView] = useState<View>("table");
  const [sourceHtml, setSourceHtml] = useState("");
  const [sourceTarget, setSourceTarget] = useState("");
  const [jobId, setJobId] = useState("");

  const bsRef = useRef<HTMLInputElement>(null);
  const debtRef = useRef<HTMLInputElement>(null);
  const leaseRef = useRef<HTMLInputElement>(null);
  const metaRef = useRef<HTMLInputElement>(null);

  const [progressPct, setProgressPct] = useState(0);
  const [elapsedSec, setElapsedSec] = useState(0);

  const handleSubmit = useCallback(async () => {
    const bsFile = bsRef.current?.files?.[0];
    const debtFile = debtRef.current?.files?.[0];
    const leaseFile = leaseRef.current?.files?.[0];
    const metaFile = metaRef.current?.files?.[0];

    if (!bsFile || !debtFile || !leaseFile) {
      setError("Please select all 3 required files");
      return;
    }

    setError("");
    setResult(null);
    setStage("uploading");
    setProgress("Uploading files...");
    setProgressPct(0);
    setElapsedSec(0);

    try {
      const { job_id } = await submitExtraction(
        bsFile, debtFile, leaseFile,
        parseFloat(marketCap) || 0,
        metaFile
      );
      setJobId(job_id);
      setStage("processing");

      const jobResult = await pollUntilDone(job_id, (status: JobStatus) => {
        const msg = status.detail ? `${status.stage}: ${status.detail}` : status.stage;
        setProgress(msg);
        setProgressPct(status.progress_pct || 0);
        setElapsedSec(status.elapsed_sec || 0);
      });

      if (jobResult.status === "error") {
        setStage("error");
        setError(jobResult.error || "Unknown error");
      } else {
        setStage("done");
        setResult(jobResult.result);
        // Fetch source HTML in background
        fetchSourceHtml(job_id).then(setSourceHtml);
      }
    } catch (e) {
      setStage("error");
      setError(e instanceof Error ? e.message : "Unknown error");
    }
  }, [marketCap]);

  const handleReset = () => {
    setStage("idle");
    setProgress("");
    setResult(null);
    setError("");
    setSourceHtml("");
    setView("table");
    [bsRef, debtRef, leaseRef, metaRef].forEach(r => { if (r.current) r.current.value = ""; });
  };

  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <div className="bg-gray-900 text-white px-6 py-4 flex items-center justify-between">
        <h1 className="text-lg font-bold tracking-tight">Capital Structure Extractor</h1>
        {result && (
          <span className="text-xs text-gray-400">
            {result.company_name} · {result.annual_period} · {result.instruments.length} instruments
          </span>
        )}
      </div>

      {/* Upload */}
      {(stage === "idle" || stage === "error") && (
        <div className="max-w-2xl mx-auto mt-10 bg-white rounded-lg shadow p-6">
          <h2 className="text-lg font-semibold mb-4">Upload SEC Filing</h2>
          <div className="grid grid-cols-1 gap-4 mb-4">
            <FileInput label="Balance Sheet (JSON)" accept=".json" inputRef={bsRef} required />
            <FileInput label="Debt Footnote (HTML)" accept=".html,.htm" inputRef={debtRef} required />
            <FileInput label="Lease Footnote (HTML)" accept=".html,.htm" inputRef={leaseRef} required />
            <FileInput label="Metadata (JSON, optional)" accept=".json" inputRef={metaRef} />
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Market Cap ($mm)</label>
              <input
                type="number"
                value={marketCap}
                onChange={e => setMarketCap(e.target.value)}
                className="block w-48 rounded border border-gray-300 px-3 py-2 text-sm"
                placeholder="0"
              />
            </div>
          </div>
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 mb-4 text-sm">{error}</div>
          )}
          <button
            onClick={handleSubmit}
            className="bg-blue-600 text-white px-6 py-2 rounded hover:bg-blue-700 text-sm font-medium"
          >
            Extract Capital Structure
          </button>
        </div>
      )}

      {/* Loading */}
      {(stage === "uploading" || stage === "processing") && (
        <div className="max-w-lg mx-auto mt-16 bg-white rounded-lg shadow p-8">
          <div className="flex items-center justify-between mb-2">
            <p className="font-semibold text-gray-800 text-sm">{progressPct < 60 ? "Processing..." : "Almost done..."}</p>
            <span className="text-xs text-gray-400">{elapsedSec > 0 ? `${elapsedSec}s` : ""}{progressPct > 0 && progressPct < 100 ? ` · ~${Math.round((elapsedSec / progressPct) * (100 - progressPct))}s remaining` : ""}</span>
          </div>
          
          {/* Progress bar */}
          <div className="w-full h-2 bg-gray-200 rounded-full overflow-hidden mb-3">
            <div 
              className="h-full bg-blue-600 rounded-full transition-all duration-700 ease-out"
              style={{ width: `${Math.max(progressPct, 2)}%` }}
            />
          </div>
          
          <p className="text-xs text-gray-500">{progress}</p>
          
          {/* Step indicators */}
          <div className="mt-4 flex gap-1">
            {["Text", "Entities", "Debt", "Leases", "Balance", "LLM"].map((step, i) => {
              const stepPct = [5, 15, 30, 50, 55, 90][i];
              const active = progressPct >= stepPct;
              const current = progressPct >= stepPct && progressPct < (i < 5 ? [15, 30, 50, 55, 90, 100][i] : 100);
              return (
                <div key={step} className="flex-1 text-center">
                  <div className={`h-1 rounded-full mb-1 ${active ? "bg-blue-600" : "bg-gray-200"} ${current ? "animate-pulse" : ""}`} />
                  <span className={`text-[9px] ${active ? "text-blue-700 font-medium" : "text-gray-400"}`}>{step}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Results */}
      {stage === "done" && result && (
        <>
          {/* Tab bar */}
          <div className="flex gap-0 bg-white border-b border-gray-200 px-6">
            {(["table", "graph", "source"] as View[]).map(v => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-5 py-3 text-sm border-b-2 transition-colors ${
                  view === v
                    ? "border-gray-900 text-gray-900 font-semibold"
                    : "border-transparent text-gray-500 hover:text-gray-700"
                }`}
              >
                {v === "table" ? "Capital Structure" : v === "graph" ? "Dependency Graph" : "Source HTML"}
              </button>
            ))}
            <button
              onClick={handleReset}
              className="ml-auto text-xs text-gray-400 hover:text-gray-600 px-4"
            >
              ← New Extraction
            </button>
          </div>

          {/* Approach */}
          {result.approach && (
            <div className="bg-blue-50 border-b border-blue-100 px-6 py-2 text-xs text-blue-800">
              <strong>LLM:</strong> {result.approach}
            </div>
          )}

          {/* TABLE VIEW */}
          {view === "table" && <TableView result={result} onClickSource={(src: string) => { setSourceTarget(src); setView("source"); }} />}

          {/* GRAPH VIEW */}
          {view === "graph" && <GraphView mermaid={result.mermaid} />}

          {/* SOURCE VIEW */}
          {view === "source" && <SourceView html={sourceHtml} highlightTarget={sourceTarget} />}

          {/* CHAT PANEL */}
          <ChatPanel jobId={jobId} onUpdate={(newResult) => setResult(newResult)} />
        </>
      )}
    </main>
  );
}

/* ─── Table View ─── */
function TableView({ result: r, onClickSource }: { result: ExtractionResult; onClickSource: (source: string) => void }) {
  const instById: Record<string, Instrument> = {};
  r.instruments.forEach(i => { instById[i.id] = i; });

  // Sort entities: subsidiaries first, parent last
  // Parent = entity whose instruments have no parent_issuer set, or matches company_name
  const parentName = (r.company_name || "").toLowerCase().replace(/[,.]?\s*(inc|corp|llc|ltd)\.?$/i, "").trim();
  
  const isParentEntity = (name: string): boolean => {
    const n = name.toLowerCase().replace(/[,.]?\s*(inc|corp|llc|ltd)\.?$/i, "").trim();
    if (!parentName) return false;
    return n === parentName || n.includes(parentName) || parentName.includes(n);
  };
  
  const sortedEntities = [...r.entities].sort((a, b) => {
    const aIsParent = isParentEntity(a);
    const bIsParent = isParentEntity(b);
    if (aIsParent && !bIsParent) return 1;
    if (!aIsParent && bIsParent) return -1;
    return 0;
  });

  // Sort instruments within each entity
  const priorityOrder: Record<string, number> = {
    "Super Senior": 0, "Senior Secured": 1, "Senior Priority Guaranteed": 2,
    "Priority Guaranteed": 3, "Guaranteed": 4, "Unsecured": 5, "Subordinated": 6,
  };
  const typeOrder = (inst: Instrument): number => {
    const label = (inst.clean_name || inst.label || "").toLowerCase();
    const t = inst.type || "";
    if (t === "revolver" || label.includes("revolv") || label.includes("credit facilit")) return 0;
    if (t === "term_loan" || label.includes("term loan")) return 1;
    if (t === "finance_lease" || label.includes("finance lease")) return 3;
    if (t === "operating_lease" || label.includes("operating lease")) return 4;
    return 2; // bonds/notes
  };

  const sortInstruments = (insts: Instrument[]) => {
    return [...insts].sort((a, b) => {
      const pa = priorityOrder[a.priority] ?? 9;
      const pb = priorityOrder[b.priority] ?? 9;
      if (pa !== pb) return pa - pb;
      const ta = typeOrder(a);
      const tb = typeOrder(b);
      if (ta !== tb) return ta - tb;
      return 0;
    });
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto">
      <p className="text-xs text-gray-400 mb-4">
        💡 Click the <strong>Source</strong> column on any row to jump to the original filing table.
      </p>
      {sortedEntities.map(entity => {
        const ids = r.entity_instruments[entity] || [];
        const insts = sortInstruments(ids.map(id => instById[id]).filter(Boolean));
        const total = insts.reduce((s, i) => s + (i.amount_mm || 0), 0);
        if (insts.length === 0) return null;

        return (
          <div key={entity} className="mb-6">
            <div className="text-sm font-bold text-gray-900 pb-1 border-b-2 border-gray-900">
              {entity}{" "}
              <span className="font-normal text-gray-400 text-xs">
                ({insts.length} instruments, ${total.toLocaleString(undefined, { maximumFractionDigits: 1 })}mm)
              </span>
            </div>
            <table className="w-full text-xs mt-1">
              <thead>
                <tr className="bg-gray-50 text-gray-500 uppercase tracking-wider">
                  <th className="text-left p-2 font-medium">Instrument</th>
                  <th className="text-right p-2 font-medium">Outstanding ($mm)</th>
                  <th className="text-right p-2 font-medium">Available ($mm)</th>
                  <th className="text-left p-2 font-medium">Coupon</th>
                  <th className="text-left p-2 font-medium">Maturity</th>
                  <th className="text-left p-2 font-medium">Priority</th>
                  <th className="text-center p-2 font-medium">Conf</th>
                  <th className="text-left p-2 font-medium">Source</th>
                  <th className="text-left p-2 font-medium max-w-[200px]">Reason</th>
                </tr>
              </thead>
              <tbody>
                {insts.map(inst => (
                  <tr
                    key={inst.id}
                    className={`border-b border-gray-100 hover:bg-gray-50 ${inst._reason ? "bg-yellow-50" : ""}`}
                  >
                    <td className="p-2">{inst.clean_name || inst.label}</td>
                    <td className="p-2 text-right font-mono">
                      {inst.amount_mm != null ? inst.amount_mm.toLocaleString(undefined, { minimumFractionDigits: 3 }) : ""}
                    </td>
                    <td className="p-2 text-right font-mono">
                      {inst.amount_available_mm != null ? inst.amount_available_mm.toLocaleString(undefined, { minimumFractionDigits: 3 }) : ""}
                    </td>
                    <td className="p-2">{inst.coupon || inst.rate || ""}</td>
                    <td className="p-2">{inst.maturity_year || ""}</td>
                    <td className="p-2"><PriorityBadge p={inst.priority} /></td>
                    <td className="p-2 text-center"><ConfBadge c={inst._confidence} /></td>
                    <td
                      className="p-2 text-[10px] text-gray-400 cursor-pointer hover:text-blue-600"
                      onClick={() => inst.source && onClickSource(inst.source)}
                    >
                      {inst.source || ""}
                    </td>
                    <td className="p-2 text-[10px] text-blue-700 max-w-[200px] truncate" title={inst._reason || ""}>
                      {inst._reason || ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}

      {/* Totals */}
      <div className="mt-4 bg-white border border-gray-200 rounded p-4 inline-block">
        <table className="text-sm">
          <tbody>
            <TotalRow label="Total Debt" value={r.total_debt_mm} bold />
            <TotalRow label="− Cash" value={-r.cash_mm} />
            <TotalRow label="Net Debt" value={r.net_debt_mm} bold border />
            <TotalRow label="+ NCI" value={r.nci_mm} />
            <TotalRow label="+ Market Cap" value={r.market_cap_mm} />
            <TotalRow label="Enterprise Value" value={r.enterprise_value_mm} bold border />
          </tbody>
        </table>
      </div>

      {/* Excluded */}
      {r.excluded.length > 0 && (
        <details className="mt-4">
          <summary className="text-sm font-semibold text-red-700 cursor-pointer">
            Excluded ({r.excluded.length} rows)
          </summary>
          <table className="w-full text-[11px] mt-1 opacity-60">
            <tbody>
              {r.excluded.map(inst => (
                <tr key={inst.id} className="border-b border-gray-100">
                  <td className="p-1">{inst.label}</td>
                  <td className="p-1 text-right font-mono">{inst.amount_mm}</td>
                  <td className="p-1 text-red-600">{inst._reason || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {/* Guarantors */}
      {r.guarantor_relationships?.length > 0 && (
        <details className="mt-4">
          <summary className="text-sm font-semibold cursor-pointer">
            Guarantor Relationships ({r.guarantor_relationships.length})
          </summary>
          <table className="w-full text-[11px] mt-1">
            <thead>
              <tr className="bg-gray-50">
                <th className="text-left p-1">Instrument/Group</th>
                <th className="text-left p-1">Issuer</th>
                <th className="text-left p-1">Guarantors</th>
                <th className="text-left p-1">Type</th>
              </tr>
            </thead>
            <tbody>
              {r.guarantor_relationships.map((g, i) => (
                <tr key={i} className="border-b border-gray-100">
                  <td className="p-1">{g.instrument_or_group}</td>
                  <td className="p-1">{g.issuer}</td>
                  <td className="p-1">{g.guarantors?.join(", ")}</td>
                  <td className="p-1">{g.guarantee_type}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

/* ─── Graph View ─── */
function GraphView({ mermaid: code }: { mermaid: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [svgContent, setSvgContent] = useState<string>("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!code) return;
    let cancelled = false;

    const loadAndRender = async () => {
      // Load mermaid if not present
      if (!(window as any).mermaid) {
        await new Promise<void>((resolve, reject) => {
          const script = document.createElement("script");
          script.src = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js";
          script.onload = () => resolve();
          script.onerror = () => reject(new Error("Failed to load mermaid"));
          document.body.appendChild(script);
        });
        (window as any).mermaid.initialize({
          startOnLoad: false,
          theme: "default",
          flowchart: { useMaxWidth: false, htmlLabels: true },
          securityLevel: "loose",
        });
      }

      try {
        // Render into a temporary detached div to avoid React DOM conflicts
        const tempDiv = document.createElement("div");
        tempDiv.id = "mermaid-temp-" + Date.now();
        tempDiv.style.position = "absolute";
        tempDiv.style.left = "-9999px";
        document.body.appendChild(tempDiv);

        const { svg } = await (window as any).mermaid.render(tempDiv.id + "-svg", code);

        // Clean up temp div
        try { document.body.removeChild(tempDiv); } catch {}

        if (!cancelled) setSvgContent(svg);
      } catch (e: any) {
        console.error("Mermaid render error:", e);
        if (!cancelled) setError("Graph render failed");
      }
    };

    loadAndRender();
    return () => { cancelled = true; };
  }, [code]);

  return (
    <div className="p-6">
      <div className="bg-white border border-gray-200 rounded p-4 overflow-auto min-h-[400px]">
        {error ? (
          <div>
            <p className="text-red-500 text-sm mb-2">{error}</p>
            <pre className="text-xs text-gray-500 whitespace-pre-wrap">{code}</pre>
          </div>
        ) : svgContent ? (
          <div dangerouslySetInnerHTML={{ __html: svgContent }} />
        ) : (
          <span className="text-sm text-gray-400">Loading graph...</span>
        )}
      </div>
    </div>
  );
}

/* ─── Source View ─── */
function SourceView({ html, highlightTarget }: { html: string; highlightTarget?: string }) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!highlightTarget || !containerRef.current) return;
    // Convert "debt_note table 0 row 5" → "source-debt_note-t0-r5"
    const match = highlightTarget.match(/(\w+)\s+table\s+(\d+)\s+row\s+(\d+)/);
    if (!match) return;
    const anchorId = `source-${match[1]}-t${match[2]}-r${match[3]}`;
    
    // Small delay to let HTML render
    setTimeout(() => {
      const el = containerRef.current?.querySelector(`#${anchorId}`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        (el as HTMLElement).style.background = "#fff3cd";
        (el as HTMLElement).style.transition = "background 0.3s";
        setTimeout(() => { (el as HTMLElement).style.background = ""; }, 3000);
      }
    }, 100);
  }, [highlightTarget, html]);

  return (
    <div className="p-6">
      <p className="text-xs text-gray-500 mb-2">
        Tables from debt and lease notes. Click &quot;Source&quot; in the table view to jump here.
        {highlightTarget && <span className="ml-2 text-blue-600">→ {highlightTarget}</span>}
      </p>
      <div
        ref={containerRef}
        className="bg-white border border-gray-200 rounded p-4 overflow-auto max-h-[80vh] text-sm"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}

/* ─── Chat Panel ─── */
function ChatPanel({ jobId, onUpdate }: { jobId: string; onUpdate: (r: ExtractionResult) => void }) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<Array<{ role: "user" | "assistant"; text: string }>>([]);

  const send = async () => {
    if (!input.trim() || loading) return;
    const msg = input.trim();
    setInput("");
    setMessages(m => [...m, { role: "user", text: msg }]);
    setLoading(true);

    try {
      const res = await sendChatCorrection(jobId, msg);
      setMessages(m => [...m, {
        role: "assistant",
        text: `${res.message}${res.corrections_applied > 0 ? ` (${res.corrections_applied} changes applied)` : ""}`
      }]);
      if (res.result) onUpdate({ ...res.result });
    } catch (e: any) {
      setMessages(m => [...m, { role: "assistant", text: `Error: ${e.message}` }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed bottom-0 right-6 z-50" style={{ width: 400 }}>
      {/* Toggle button */}
      <button
        onClick={() => setOpen(!open)}
        className="w-full bg-gray-900 text-white px-4 py-2 rounded-t-lg text-sm font-medium flex items-center justify-between hover:bg-gray-800"
      >
        <span>💬 Corrections Chat</span>
        <span className="text-xs opacity-60">{open ? "▼" : "▲"}</span>
      </button>

      {open && (
        <div className="bg-white border border-gray-200 border-t-0 shadow-lg flex flex-col" style={{ height: 350 }}>
          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-3 space-y-2 text-xs">
            {messages.length === 0 && (
              <div className="text-gray-400 text-center mt-8">
                <p>Ask to correct the extraction:</p>
                <p className="mt-2 italic">&quot;The 5.75% notes should be under Bausch + Lomb&quot;</p>
                <p className="italic">&quot;Add the operating leases from the lease sheet&quot;</p>
                <p className="italic">&quot;Remove the total debt row, it&apos;s double counting&quot;</p>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                <div className={`max-w-[85%] px-3 py-2 rounded-lg ${
                  m.role === "user"
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-800"
                }`}>
                  {m.text}
                </div>
              </div>
            ))}
            {loading && (
              <div className="flex justify-start">
                <div className="bg-gray-100 text-gray-500 px-3 py-2 rounded-lg animate-pulse">Thinking...</div>
              </div>
            )}
          </div>

          {/* Input */}
          <div className="border-t border-gray-200 p-2 flex gap-2">
            <input
              type="text"
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key === "Enter" && send()}
              placeholder="Describe what to fix..."
              className="flex-1 border border-gray-300 rounded px-3 py-1.5 text-sm focus:outline-none focus:border-blue-400"
              disabled={loading}
            />
            <button
              onClick={send}
              disabled={loading || !input.trim()}
              className="bg-blue-600 text-white px-3 py-1.5 rounded text-sm font-medium disabled:opacity-40 hover:bg-blue-700"
            >
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── Shared Components ─── */
function PriorityBadge({ p }: { p: string }) {
  const colors: Record<string, string> = {
    "Senior Secured": "bg-green-100 text-green-800",
    Guaranteed: "bg-orange-100 text-orange-800",
    Unsecured: "bg-purple-100 text-purple-800",
    Subordinated: "bg-red-100 text-red-800",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${colors[p] || "bg-gray-100 text-gray-600"}`}>
      {p}
    </span>
  );
}

function ConfBadge({ c }: { c?: number | null }) {
  if (c == null) return null;
  const color = c >= 0.8 ? "text-green-600" : c >= 0.5 ? "text-amber-500" : "text-red-500";
  return <span className={`text-[10px] font-semibold ${color}`}>{Math.round(c * 100)}%</span>;
}

function TotalRow({ label, value, bold, border }: { label: string; value: number; bold?: boolean; border?: boolean }) {
  return (
    <tr className={border ? "border-t-2 border-gray-900" : ""}>
      <td className={`pr-6 py-1 ${bold ? "font-bold" : ""}`}>{label}</td>
      <td className={`text-right font-mono py-1 ${bold ? "font-bold" : ""}`}>
        ${value?.toLocaleString(undefined, { minimumFractionDigits: 1 })}mm
      </td>
    </tr>
  );
}

function FileInput({
  label, accept, inputRef, required,
}: {
  label: string; accept: string; inputRef: React.RefObject<HTMLInputElement | null>; required?: boolean;
}) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 mb-1">
        {label} {required && <span className="text-red-500">*</span>}
      </label>
      <input
        type="file"
        accept={accept}
        ref={inputRef}
        className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
      />
    </div>
  );
}

function Spinner() {
  return (
    <svg className="animate-spin h-8 w-8 text-blue-600 mx-auto" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}
