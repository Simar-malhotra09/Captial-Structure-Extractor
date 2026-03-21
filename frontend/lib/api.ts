const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface JobResponse {
  job_id: string;
  status: string;
}

export interface Instrument {
  id: string;
  label: string;
  amount_mm: number | null;
  amount_concept?: string;
  amount_available_mm?: number | null;
  rate?: string | null;
  type: string;
  priority: string;
  entity?: string;
  source?: string;
  clean_name?: string;
  coupon?: string;
  maturity_year?: string;
  issue_date?: string;
  footnotes?: string[];
  _reason?: string;
  _confidence?: number | null;
  _excluded?: boolean;
}

export interface GuarantorRelationship {
  instrument_or_group: string;
  issuer: string;
  guarantors: string[];
  guarantee_type: string;
  narrative_quote?: string;
}

export interface ExtractionResult {
  company_name: string;
  approach: string;
  entities: string[];
  instruments: Instrument[];
  excluded: Instrument[];
  entity_instruments: Record<string, string[]>;
  mermaid: string;
  guarantor_relationships: GuarantorRelationship[];
  total_debt_mm: number;
  cash_mm: number;
  nci_mm: number;
  net_debt_mm: number;
  market_cap_mm: number;
  enterprise_value_mm: number;
  annual_period: number;
  lease_count: number;
}

export interface JobStatus {
  id: string;
  status: "pending" | "running" | "done" | "error";
  stage: string;
  detail: string;
  progress_pct: number;
  elapsed_sec: number;
  created_at: string;
  result: ExtractionResult | null;
  error: string | null;
}

export async function submitExtraction(
  balanceSheet: File,
  debtNote: File,
  leaseNote: File,
  marketCap: number = 0,
  metadata?: File
): Promise<JobResponse> {
  const form = new FormData();
  form.append("balance_sheet", balanceSheet);
  form.append("debt_note", debtNote);
  form.append("lease_note", leaseNote);
  form.append("market_cap", marketCap.toString());
  if (metadata) form.append("metadata", metadata);

  const res = await fetch(`${API_BASE}/api/extract`, {
    method: "POST",
    body: form,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Upload failed (${res.status}): ${text}`);
  }

  return res.json();
}

export async function pollJob(jobId: string): Promise<JobStatus> {
  const res = await fetch(`${API_BASE}/api/jobs/${jobId}`);
  if (!res.ok) throw new Error(`Poll failed (${res.status})`);
  return res.json();
}

export async function pollUntilDone(
  jobId: string,
  onProgress?: (status: JobStatus) => void,
  intervalMs: number = 1500
): Promise<JobStatus> {
  while (true) {
    const status = await pollJob(jobId);
    onProgress?.(status);
    if (status.status === "done" || status.status === "error") return status;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export async function fetchSourceHtml(jobId: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/jobs/${jobId}/source`);
  if (!res.ok) return "";
  return res.text();
}

export interface ChatResponse {
  corrections_applied: number;
  message: string;
  result: ExtractionResult;
}

export async function sendChatCorrection(jobId: string, message: string): Promise<ChatResponse> {
  const res = await fetch(`${API_BASE}/api/jobs/${jobId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Chat failed (${res.status}): ${text}`);
  }
  return res.json();
}
