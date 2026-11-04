import type { Job, JobScope, PolicyDraft, PolicyResult, RepositoryFilters, TagDefinition, TagStats } from "./api";

export const emptyFilters: RepositoryFilters = { q: "", language: "", min_stars: 0, sort: "stars", policy_id: "", tag_id: "", tag_ids: [], retention_source: "" };

export function selectedTagIds(filters: RepositoryFilters): string[] {
  return filters.tag_ids?.length ? filters.tag_ids : filters.tag_id ? [filters.tag_id] : [];
}

/** Multiple tags match any selected label within the same policy. */
export function toggleTagFilter(filters: RepositoryFilters, policyId: string, tagId: string): RepositoryFilters {
  const current = filters.policy_id === policyId ? selectedTagIds(filters) : [];
  const tagIds = !tagId ? [] : current.includes(tagId) ? current.filter(id => id !== tagId) : [...current, tagId];
  return { ...filters, policy_id: policyId, tag_id: "", tag_ids: tagIds };
}

export function newPolicyDraft(): PolicyDraft {
  return { name: "", description: "", mode: "multi_label", threshold: 0.8, evidence_kinds: ["metadata", "readme"],
    tags: [{ id: crypto.randomUUID(), label: "", description: "", threshold: 0.8 }] };
}

/** Old policies had only one default; normalize once before editing or previewing. */
export function tagThreshold(tag: Pick<TagDefinition, "threshold">, fallback = 0.8) {
  return typeof tag.threshold === "number" ? tag.threshold : Number.isFinite(fallback) ? fallback : 0.8;
}

export function thresholdMap(policy: Pick<PolicyDraft, "tags" | "threshold">): Record<string, number> {
  return Object.fromEntries(policy.tags.map(tag => [tag.id, tagThreshold(tag, policy.threshold)]));
}

export function thresholdsValid(thresholds: Record<string, number>) {
  return Object.keys(thresholds).length > 0 && Object.values(thresholds).every(value => Number.isFinite(value) && value >= 0 && value <= 1);
}

/** Threshold previews reuse model scores and preserve explicit manual decisions. */
export function previewResult(row: PolicyResult, mode: PolicyDraft["mode"], thresholds: Record<string, number>) {
  return row.results.map(result => {
    const threshold = thresholds[result.tag_id];
    const eligible = mode === "multi_label" || (row.winner_tag_id != null && result.tag_id === row.winner_tag_id);
    const manual = typeof result.manual_applied === "boolean" ? result.manual_applied : null;
    return { ...result, threshold, applied: manual ?? (eligible && Number.isFinite(threshold) && result.score != null && result.score >= threshold) };
  });
}

export function previewCount(stats: TagStats, threshold: number): number | null {
  if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1) return null;
  // The server already restricts single-label distributions to original winners.
  return (stats.manual_applied_count || 0) + stats.distribution.reduce((count, bin) => count + (bin.score >= threshold ? bin.count : 0), 0);
}

export function policyValidation(draft: PolicyDraft): string | null {
  if (!draft.name.trim()) return "请输入策略名称";
  if (!draft.tags.length) return "请至少添加一个标签";
  if (draft.tags.some(tag => !tag.label.trim() || !tag.description.trim())) return "请填写每个标签的名称和判断规则";
  if (!thresholdsValid(thresholdMap(draft))) return "每个标签的阈值应在 0 到 1 之间";
  const labels = draft.tags.map(tag => tag.label.trim().toLocaleLowerCase());
  if (new Set(labels).size !== labels.length) return "标签名称不能重复";
  return null;
}

/** Explicit scope creation is called only by the run-dialog submit handler. */
export function createJobScope(mode: "selected" | "filtered", selectedIds: number[], filters: RepositoryFilters, limit: number): JobScope {
  if (!Number.isInteger(limit) || limit < 1) throw new Error("分类数量必须是正整数");
  if (mode === "selected") {
    if (!selectedIds.length) throw new Error("请先选择仓库");
    return { repository_ids: [...new Set(selectedIds)], limit };
  }
  return { filters: { ...filters, ...(filters.tag_ids ? { tag_ids: [...filters.tag_ids] } : {}) }, limit };
}

export function jobProgress(job: Pick<Job, "total" | "completed" | "failed" | "skipped">) {
  return job.total > 0 ? Math.min(100, Math.max(0, ((job.completed + job.failed + job.skipped) / job.total) * 100)) : 0;
}
