/** Shared HTTP shapes mirror web/CONTRACT.md. No credentials enter this client. */
export interface TagDefinition { id: string; label: string; description: string; threshold: number }
export interface AppliedTag { policy_id: string; tag_id: string; label: string; score: number; source?: "model" | "manual" }
export interface CandidateSource {
  source_repo: string; source_url: string; source_line?: number; original_url?: string;
}
export interface CandidateSupplement {
  reason: string; sources: CandidateSource[];
  metadata_readme_conflict?: { metadata_description: string; note: string };
}
export interface Repository {
  id: number; full_name: string; url: string; description: string | null;
  stars: number; language: string | null; topics: string[]; license: string | null;
  default_branch: string | null; created_at: string | null; updated_at: string | null;
  pushed_at: string | null; archived: boolean; agent_probability: number | null; tags: AppliedTag[];
  prescreen_probability?: number | null; retention_source?: string; protected_keep?: boolean;
  original_prescreen_decision?: string;
}
export interface Evidence {
  id: string; repository_id: number; kind: string; version: string; source_key: string; source_url: string | null;
  content_hash: string; captured_at: string; content: unknown; provenance: Record<string, unknown>;
}
export interface TagResult {
  tag_id: string; label: string; score: number | null; applied: boolean; source?: "model" | "manual" | null;
  model_score?: number | null; model_applied?: boolean; manual_applied?: boolean | null;
}
export interface AnnotationTag {
  tag_id: string; label: string; description: string; model_score: number | null; model_applied: boolean;
  manual_applied: boolean | null; score: number | null; applied: boolean; source?: "model" | "manual" | null;
}
export interface AnnotationPolicy {
  policy_id: string; policy_name: string; policy_revision: number; mode: "multi_label" | "single_label"; tags: AnnotationTag[];
}
export interface Classification {
  policy_id: string; policy_name: string; policy_revision: number; job_id: string;
  model: string; created_at: string; evidence_ids: string[]; results: TagResult[];
}
export interface RepositoryDetail extends Repository {
  evidence: Evidence[]; classifications: Classification[]; supplement?: CandidateSupplement | null;
}
export interface PolicyDraft {
  name: string; description: string; mode: "multi_label" | "single_label";
  threshold: number; tags: TagDefinition[]; evidence_kinds: string[];
}
export interface Policy extends PolicyDraft {
  id: string; revision: number; created_at: string; updated_at: string;
  result_count?: number; applied_count?: number;
}
export interface PolicyResult {
  repository_id: number; full_name: string; url: string; stars: number; language: string | null;
  model: string | null; scored_at: string | null; source_job_id: string | null; source_policy_revision: number;
  evidence_ids: string[]; winner_tag_id: string | null;
  results: (TagResult & { threshold: number })[];
}
export interface TagStats {
  tag_id: string; label: string; threshold: number; scored_count: number; applied_count: number;
  manual_applied_count?: number;
  distribution: { score: number; count: number }[];
}
export interface PolicyResultPage {
  policy: Policy; items: PolicyResult[]; total: number; page: number; page_size: number; tag_stats: TagStats[];
}
export type JobStatus = "queued" | "running" | "paused" | "completed" | "completed_with_errors" | "failed" | "cancelled";
export interface Job {
  id: string; policy_id: string; policy_name: string; policy_revision: number; status: JobStatus;
  model?: string;
  evidence_kinds?: string[];
  total: number; completed: number; failed: number; skipped: number; cost: number | null;
  unknown_cost_count: number; created_at: string; updated_at: string; error: string | null;
}
export interface JobItem { id: number; repository_id: number; full_name?: string; status: string; error?: string | null }
export interface JobDetail extends Job { items: JobItem[] }
export interface Stats {
  total_repositories: number; tagged_repositories: number; policy_count: number; active_jobs: number;
  languages: { name: string; count: number }[];
  dataset: {
    source_count: number; selected_count: number; threshold: number; imported_at: string;
    dataset_id?: string; label?: string; screening_stage?: string; fine_screened?: boolean;
    protected_keep_count?: number; awesome_supplement_count?: number;
  };
}
export type ClassifierModel = "typesafe/jev-1.14" | "glm-5.3";
export interface ModelOption { id: ClassifierModel; label: string; configured: boolean }
export interface Health { status: string; jev_configured: boolean; model: string; glm_configured?: boolean; models?: ModelOption[] }
export interface PolicyChatMessage { role: "user" | "assistant"; content: string }
export interface GeneratedPolicy {
  message: string; policy: PolicyDraft; model: string; usage: Record<string, unknown>;
  cost: number | null; generation_id: string;
}
export interface RepositoryFilters {
  q: string; language: string; min_stars: number; sort: "stars" | "updated" | "name";
  policy_id: string; tag_id: string; tag_ids?: string[]; retention_source?: string;
}
export interface RepositoryPage { items: Repository[]; total: number; page: number; page_size: number }
export interface JobScope { repository_ids?: number[]; filters?: RepositoryFilters; limit?: number }

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); this.name = "ApiError"; }
}

export const SESSION_EXPIRED_EVENT = "agendex:session-expired";

async function checkResponse(response: Response, authRequest = false) {
  if (response.ok) return;
  if (response.status === 401 && !authRequest && typeof window !== "undefined") {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }
  let message = `请求失败（${response.status}）`;
  try {
    const data = await response.json();
    if (typeof data.detail === "string") message = data.detail;
    else if (Array.isArray(data.detail)) message = data.detail.map((item: { msg?: string }) => item.msg || "输入不正确").join("；");
  } catch { /* A proxy may return an HTML error rather than JSON. */ }
  throw new ApiError(message, response.status);
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", "X-Agendex-Client": "web", ...options.headers },
  });
  await checkResponse(response, path.startsWith("/auth/"));
  return response.json() as Promise<T>;
}

/** Export endpoints provide the full result set, independently of table pagination. */
async function downloadCsv(path: string, fallbackName: string) {
  const response = await fetch(`/api${path}`, { headers: { "X-Agendex-Client": "web" } });
  await checkResponse(response);
  if (!response.headers.get("Content-Type")?.includes("text/csv")) throw new Error("下载失败，请刷新页面后重试。");
  const disposition = response.headers.get("Content-Disposition") || "";
  let filename = disposition.match(/filename="([^"]+)"/i)?.[1] || fallbackName;
  const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encodedName) {
    try { filename = decodeURIComponent(encodedName); } catch { /* Use the ASCII filename if malformed. */ }
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.hidden = true;
  document.body.append(link);
  try { link.click(); }
  finally { link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); }
}

export function queryString(values: Record<string, string | number | undefined>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== "" && value != null) params.set(key, String(value)); });
  return params.toString();
}

export const api = {
  health: (signal?: AbortSignal) => request<Health>("/health", { signal }),
  stats: (signal?: AbortSignal) => request<Stats>("/stats", { signal }),
  repositories: (filters: RepositoryFilters, page: number, pageSize: number, signal?: AbortSignal) =>
    request<RepositoryPage>(`/repositories?${queryString({ ...filters, tag_id: filters.tag_ids?.length ? "" : filters.tag_id,
      tag_ids: filters.tag_ids?.join(","), page, page_size: pageSize })}`, { signal }),
  downloadRepositories: () => downloadCsv("/repositories/export.csv", "agendex-repositories.csv"),
  repository: (id: number, signal?: AbortSignal) => request<RepositoryDetail>(`/repositories/${id}`, { signal }),
  repositoryAnnotations: (id: number, signal?: AbortSignal) => request<{ items: AnnotationPolicy[] }>(`/repositories/${id}/annotations`, { signal }),
  saveRepositoryAnnotations: (id: number, policyId: string, baseRevision: number, overrides: { tag_id: string; applied: boolean }[]) =>
    request<RepositoryDetail>(`/repositories/${id}/annotations`, { method: "PUT", body: JSON.stringify({ policy_id: policyId, base_revision: baseRevision, overrides }) }),
  policies: (signal?: AbortSignal) => request<{ items: Policy[] }>("/policies", { signal }),
  policyVersions: (id: string, signal?: AbortSignal) => request<{ items: Policy[] }>(`/policies/${id}/versions`, { signal }),
  policyResults: (id: string, revision: number | undefined, q: string, page: number, signal?: AbortSignal) =>
    request<PolicyResultPage>(`/policies/${id}/results?${queryString({ revision, q, page, page_size: 20 })}`, { signal }),
  downloadPolicyResults: (id: string, revision: number, q: string, thresholds?: Record<string, number>) =>
    downloadCsv(`/policies/${encodeURIComponent(id)}/results/export.csv?${queryString({ revision, q, thresholds: thresholds ? JSON.stringify(thresholds) : undefined })}`, `agendex-results-v${revision}.csv`),
  applyThresholds: (id: string, baseRevision: number, thresholds: Record<string, number>) => request<Policy>(`/policies/${id}/thresholds`, {
    method: "POST", body: JSON.stringify({ base_revision: baseRevision, thresholds }),
  }),
  restorePolicy: (id: string, revision: number) => request<Policy>(`/policies/${id}/restore/${revision}`, { method: "POST" }),
  savePolicy: (draft: PolicyDraft, id?: string) => request<Policy>(id ? `/policies/${id}` : "/policies", {
    method: id ? "PUT" : "POST", body: JSON.stringify(draft),
  }),
  generatePolicy: (messages: PolicyChatMessage[], draft: PolicyDraft, signal?: AbortSignal) => request<GeneratedPolicy>("/policies/generate", {
    method: "POST", body: JSON.stringify({ messages, draft }), signal,
  }),
  deletePolicy: (id: string) => request<{ ok: boolean }>(`/policies/${id}`, { method: "DELETE" }),
  jobs: (signal?: AbortSignal) => request<{ items: Job[] }>("/jobs", { signal }),
  job: (id: string, signal?: AbortSignal) => request<JobDetail>(`/jobs/${id}`, { signal }),
  createJob: (policyId: string, scope: JobScope, skipCompleted: boolean, model?: ClassifierModel, evidenceKinds?: string[]) => request<Job>("/jobs", {
    method: "POST", body: JSON.stringify({ policy_id: policyId, scope, skip_completed: skipCompleted, model, evidence_kinds: evidenceKinds }),
  }),
  jobAction: (id: string, action: "pause" | "resume" | "cancel") => request<Job>(`/jobs/${id}/${action}`, { method: "POST" }),
};
