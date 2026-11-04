// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type Policy, type PolicyResult, type PolicyResultPage } from "@/lib/api";
import { PolicyResultsDrawer } from "./policy-results";

vi.mock("@/lib/api", () => ({ api: {
  policyVersions: vi.fn(), policyResults: vi.fn(), applyThresholds: vi.fn(), restorePolicy: vi.fn(), createJob: vi.fn(),
} }));

const originalPolicy: Policy = {
  id: "p1", name: "实现形式", description: "仅依据明确证据", mode: "multi_label", threshold: 0.8,
  tags: [{ id: "own", label: "自主实现", description: "自行控制循环", threshold: 0.8 }],
  evidence_kinds: ["metadata"], revision: 1, created_at: "2026-10-31", updated_at: "2026-10-31",
  result_count: 2, applied_count: 1,
};
const rawRows: PolicyResult[] = [0.65, 0.9].map((score, index) => ({
  repository_id: index + 1, full_name: `example/repo${index + 1}`, url: `https://github.com/example/repo${index + 1}`,
  stars: 10, language: "Python", model: "typesafe/jev-1.14", scored_at: "2026-10-31",
  source_job_id: "j1", source_policy_revision: 1, evidence_ids: ["metadata"], winner_tag_id: null,
  results: [{ tag_id: "own", label: "自主实现", score, threshold: 0.8, applied: score >= 0.8 }],
}));
function resultPage(policy: Policy): PolicyResultPage {
  return { policy, items: rawRows, total: 2, page: 1, page_size: 20, tag_stats: [{
    tag_id: "own", label: "自主实现", threshold: policy.tags[0].threshold, scored_count: 2, applied_count: 1,
    distribution: [{ score: 0.65, count: 1 }, { score: 0.9, count: 1 }],
  }] };
}

let previousScrollIntoView: typeof Element.prototype.scrollIntoView;
beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  previousScrollIntoView = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); Element.prototype.scrollIntoView = previousScrollIntoView; });

describe("策略保存结果", () => {
  it("changing a threshold immediately previews counts and tags, and only explicit apply saves a version", async () => {
    let current = originalPolicy;
    const changed = vi.fn();
    vi.mocked(api.policyVersions).mockImplementation(async () => ({ items: current.revision === 1 ? [current] : [current, originalPolicy] }));
    vi.mocked(api.policyResults).mockImplementation(async () => resultPage(current));
    vi.mocked(api.applyThresholds).mockImplementation(async (_id, _revision, thresholds) => {
      current = { ...originalPolicy, revision: 2, tags: [{ ...originalPolicy.tags[0], threshold: thresholds.own }] };
      return current;
    });
    render(<PolicyResultsDrawer policy={originalPolicy} refresh={0} onClose={vi.fn()} onPolicyChanged={changed} onRepository={vi.fn()} />);
    await screen.findByText("example/repo1");
    expect(screen.getByText("未打标签")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("自主实现 阈值"), { target: { value: "0.6" } });
    expect(screen.queryByText("未打标签")).toBeNull();
    expect(screen.getByLabelText("标签阈值").textContent).toContain("2 个保留");
    expect(api.applyThresholds).not.toHaveBeenCalled();
    expect(api.createJob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "应用阈值" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith(current));
    expect(api.applyThresholds).toHaveBeenCalledWith("p1", 1, { own: 0.6 });
    expect(api.createJob).not.toHaveBeenCalled();
  });

  it("historical versions are read-only and can be restored without a new scoring task", async () => {
    let current: Policy = { ...originalPolicy, revision: 2, tags: [{ ...originalPolicy.tags[0], threshold: 0.7 }] };
    const changed = vi.fn();
    vi.mocked(api.policyVersions).mockImplementation(async () => ({ items: [current, originalPolicy] }));
    vi.mocked(api.policyResults).mockImplementation(async (_id, revision) => resultPage(revision === 1 ? originalPolicy : current));
    vi.mocked(api.restorePolicy).mockImplementation(async () => {
      current = { ...originalPolicy, revision: 3 };
      return current;
    });
    render(<PolicyResultsDrawer policy={current} refresh={0} onClose={vi.fn()} onPolicyChanged={changed} onRepository={vi.fn()} />);
    await screen.findByText("example/repo1");
    fireEvent.keyDown(screen.getByRole("combobox", { name: "策略版本" }), { key: "Enter" });
    const historical = await screen.findByRole("option", { name: /v1/ });
    fireEvent.keyDown(historical, { key: "Enter" });
    const restore = await screen.findByRole("button", { name: "复用此版本" });
    expect((screen.getByLabelText("自主实现 阈值") as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "应用阈值" })).toBeNull();
    fireEvent.click(restore);
    await waitFor(() => expect(changed).toHaveBeenCalledWith(current));
    expect(api.restorePolicy).toHaveBeenCalledWith("p1", 1);
    expect(api.applyThresholds).not.toHaveBeenCalled();
    expect(api.createJob).not.toHaveBeenCalled();
  });
});
