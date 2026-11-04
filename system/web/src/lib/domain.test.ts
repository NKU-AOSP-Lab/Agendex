import { describe, expect, it } from "vitest";
import { createJobScope, emptyFilters, jobProgress, newPolicyDraft, policyValidation, previewCount, previewResult, selectedTagIds, thresholdMap, toggleTagFilter } from "./domain";
import type { PolicyResult, TagDefinition } from "./api";

describe("policy drafts", () => {
  it("starts with no predefined classification rules and an independent tag threshold", () => {
    const first = newPolicyDraft();
    const second = newPolicyDraft();
    expect(first.name).toBe("");
    expect(first.tags.map(tag => [tag.label, tag.description])).toEqual([["", ""]]);
    expect(first.threshold).toBe(0.8);
    expect(first.tags[0].threshold).toBe(0.8);
    expect(first.evidence_kinds).toEqual(["metadata", "readme"]);
    expect(first.tags[0].id).not.toBe(second.tags[0].id);
  });

  it("requires clear rules, finite thresholds, and unique label names", () => {
    const draft = newPolicyDraft();
    draft.name = "用途";
    draft.tags = [{ id: "code", label: "开发", description: "明确以开发代码为主要任务。", threshold: 0.8 }];
    expect(policyValidation(draft)).toBeNull();
    expect(policyValidation({ ...draft, tags: [{ ...draft.tags[0], threshold: NaN }] })).not.toBeNull();
    expect(policyValidation({ ...draft, tags: [{ ...draft.tags[0], threshold: 1.01 }] })).not.toBeNull();
    expect(policyValidation({ ...draft, tags: [{ ...draft.tags[0], description: " " }] })).not.toBeNull();
    expect(policyValidation({ ...draft, tags: [...draft.tags, { id: "other", label: " 开发 ", description: "重复标签", threshold: 0.4 }] })).toContain("重复");
  });

  it("only uses the legacy strategy threshold for tags that have no threshold", () => {
    const legacy = { id: "legacy", label: "旧标签", description: "旧规则" } as TagDefinition;
    const current = { id: "current", label: "新标签", description: "新规则", threshold: 0.6 };
    expect(thresholdMap({ threshold: 0.7, tags: [legacy, current] })).toEqual({ legacy: 0.7, current: 0.6 });
  });


});

describe("saved-score threshold preview", () => {
  const row: PolicyResult = {
    repository_id: 1, full_name: "example/agent", url: "https://github.com/example/agent", stars: 10, language: "Python",
    model: "typesafe/jev-1.14", scored_at: "2026-10-31", source_job_id: "job", source_policy_revision: 1,
    evidence_ids: ["metadata-v1"], winner_tag_id: "code",
    results: [
      { tag_id: "code", label: "开发", score: 0.4, threshold: 0.8, applied: false },
      { tag_id: "research", label: "研究", score: 0.95, threshold: 0.8, applied: true },
    ],
  };

  it("recomputes each multi-label decision from its own threshold and leaves saved results unchanged", () => {
    const preview = previewResult(row, "multi_label", { code: 0.4, research: 0.99 });
    expect(preview.map(result => result.applied)).toEqual([true, false]);
    expect(row.results.map(result => result.applied)).toEqual([false, true]);
    expect(preview.map(result => result.score)).toEqual([0.4, 0.95]);
  });

  it("retains only the original single-label winner, even when a different score is larger", () => {
    expect(previewResult(row, "single_label", { code: 0.4, research: 0.1 }).map(result => result.applied)).toEqual([true, false]);
    expect(previewResult(row, "single_label", { code: 0.41, research: 0.1 }).map(result => result.applied)).toEqual([false, false]);
  });

  it("never turns an original none answer into a tag when lowering thresholds", () => {
    expect(previewResult({ ...row, winner_tag_id: null }, "single_label", { code: 0, research: 0 }).every(result => !result.applied)).toBe(true);
  });

  it("previews global counts from eligible score bins and includes the threshold boundary", () => {
    const stats = { tag_id: "code", label: "开发", threshold: 0.8, scored_count: 20, applied_count: 3,
      distribution: [{ score: 0.2, count: 4 }, { score: 0.8, count: 2 }, { score: 0.9, count: 1 }] };
    expect(previewCount(stats, 0.8)).toBe(3);
    expect(previewCount(stats, 0.2)).toBe(7);
    expect(previewCount(stats, 0.91)).toBe(0);
    expect(previewCount(stats, NaN)).toBeNull();
  });

  it("keeps manual decisions fixed at every threshold, including zero and a different model winner", () => {
    const manual: PolicyResult = { ...row, results: [
      { ...row.results[0], source: "manual", score: 0, manual_applied: false },
      { ...row.results[1], source: "manual", score: 1, manual_applied: true },
    ] };
    expect(previewResult(manual, "single_label", { code: 0, research: 1 }).map(result => result.applied)).toEqual([false, true]);
    const unscored = { ...row, results: [{ ...row.results[0], score: null }] };
    expect(previewResult(unscored, "multi_label", { code: 0 })[0].applied).toBe(false);
    expect(previewCount({ tag_id: "code", label: "开发", threshold: 1, scored_count: 0, applied_count: 2, manual_applied_count: 2, distribution: [] }, 1)).toBe(2);
  });
});

describe("explicit classification scope", () => {
  it("sends only selected IDs in selected mode and never mutates the selection", () => {
    const ids = [3, 7, 3];
    const scope = createJobScope("selected", ids, { ...emptyFilters, language: "Python" }, 2);
    expect(scope).toEqual({ repository_ids: [3, 7], limit: 2 });
    expect(ids).toEqual([3, 7, 3]);
    expect(scope).not.toHaveProperty("filters");
  });

  it("preserves all filters when choosing the filtered result set", () => {
    const filters = { ...emptyFilters, q: "research", min_stars: 100, policy_id: "p1", tag_ids: ["code", "browser"] };
    const scope = createJobScope("filtered", [3, 7], filters, 100);
    expect(scope).toEqual({ filters, limit: 100 });
    expect(scope.filters).not.toBe(filters);
    expect(scope.filters?.tag_ids).not.toBe(filters.tag_ids);
    expect(scope).not.toHaveProperty("repository_ids");
  });

  it("rejects empty selections and invalid or unbounded run amounts", () => {
    expect(() => createJobScope("selected", [], emptyFilters, 10)).toThrow();
    for (const count of [0, -1, 1.2, NaN, Infinity]) {
      expect(() => createJobScope("filtered", [], emptyFilters, count)).toThrow();
    }
  });
});

describe("multiple tag filters", () => {
  it("toggles labels independently, clears them, and starts fresh for a different policy", () => {
    const first = toggleTagFilter({ ...emptyFilters, language: "Python" }, "p1", "code");
    const second = toggleTagFilter(first, "p1", "browser");
    expect(selectedTagIds(second)).toEqual(["code", "browser"]);
    expect(selectedTagIds(toggleTagFilter(second, "p1", "code"))).toEqual(["browser"]);
    expect(selectedTagIds(toggleTagFilter(second, "p1", ""))).toEqual([]);
    const different = toggleTagFilter(second, "p2", "cli");
    expect(different).toMatchObject({ policy_id: "p2", tag_id: "", tag_ids: ["cli"], language: "Python" });
    expect(first.tag_ids).toEqual(["code"]);
  });
});

describe("progress", () => {
  it("counts terminal outcomes while leaving unfinished tasks incomplete", () => {
    expect(jobProgress({ total: 10, completed: 4, failed: 1, skipped: 2 })).toBe(70);
    expect(jobProgress({ total: 0, completed: 0, failed: 0, skipped: 0 })).toBe(0);
    expect(jobProgress({ total: 10, completed: 12, failed: 0, skipped: 0 })).toBe(100);
  });
});
