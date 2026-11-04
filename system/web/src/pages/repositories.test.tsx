// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type Policy, type Stats } from "@/lib/api";
import { RepositoriesPage } from "./repositories";

vi.mock("@/lib/api", () => ({ api: { repositories: vi.fn(), downloadRepositories: vi.fn() } }));

const policies: Policy[] = [
  { id: "scenarios", name: "用途", description: "", mode: "multi_label", threshold: .5, revision: 1, created_at: "", updated_at: "", evidence_kinds: ["metadata", "readme"], result_count: 10,
    tags: [{ id: "code", label: "代码开发", description: "Develops code.", threshold: .5 }, { id: "browser", label: "网页操作", description: "Operates websites.", threshold: .5 }] },
  { id: "interfaces", name: "交互", description: "", mode: "multi_label", threshold: .5, revision: 1, created_at: "", updated_at: "", evidence_kinds: ["metadata", "readme"], result_count: 10,
    tags: [{ id: "cli", label: "CLI", description: "Provides command line access.", threshold: .5 }] },
];
const stats: Stats = { total_repositories: 10, tagged_repositories: 10, policy_count: 2, active_jobs: 0, languages: [], dataset: { source_count: 10, selected_count: 10, threshold: .5, imported_at: "" } };
let originalScroll: typeof Element.prototype.scrollIntoView;
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.repositories).mockResolvedValue({ items: [], total: 10, page: 1, page_size: 25 });
  originalScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); Element.prototype.scrollIntoView = originalScroll; });

describe("目录标签多选", () => {
  it("多选标签随任务范围提交；下拉框可独立取消、清空，换策略后清除选择", async () => {
    const run = vi.fn();
    render(<RepositoriesPage policies={policies} policiesLoading={false} policiesError={null} onReloadPolicies={vi.fn()} onCreatePolicy={vi.fn()} stats={stats} refresh={0} onRun={run} onDetails={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "用途" }));
    fireEvent.click(screen.getByRole("button", { name: "代码开发" }));
    fireEvent.click(screen.getByRole("button", { name: "网页操作" }));
    expect(screen.getByRole("button", { name: "代码开发" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "网页操作" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "新建分类任务" }));
    expect(run.mock.calls[0][0].filters.tag_ids).toEqual(["code", "browser"]);
    fireEvent.keyDown(screen.getByRole("button", { name: "标签筛选" }), { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: "代码开发" }));
    expect(screen.getByRole("menuitemcheckbox", { name: "网页操作" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(screen.getByRole("menuitemcheckbox", { name: "网页操作" }).getAttribute("aria-checked")).toBe("false");
    fireEvent.keyDown(screen.getByRole("menuitemcheckbox", { name: "网页操作" }), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "代码开发" }));
    fireEvent.click(screen.getByRole("button", { name: "交互" }));
    expect(screen.getByRole("button", { name: "全部标签" }).getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(vi.mocked(api.repositories).mock.lastCall?.[0]).toMatchObject({ policy_id: "interfaces", tag_ids: [], tag_id: "" }));
  });
});
