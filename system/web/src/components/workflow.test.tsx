// @vitest-environment jsdom
/** 用户操作测试：保存规则与发起付费任务必须由明确的提交动作触发。 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type GeneratedPolicy, type Job, type Policy } from "@/lib/api";
import { emptyFilters } from "@/lib/domain";
import { PolicyEditor } from "./policy-editor";
import { RunDialog } from "./run-dialog";
import { ManualTagEditor } from "./manual-tag-editor";

vi.mock("@/lib/api", () => ({ api: { savePolicy: vi.fn(), createJob: vi.fn(), generatePolicy: vi.fn(), repositoryAnnotations: vi.fn(), saveRepositoryAnnotations: vi.fn() } }));

const policy: Policy = {
  id: "policy-fixture", name: "实现形式", description: "只依据明确证据", mode: "multi_label",
  threshold: 0.8, tags: [{ id: "own-loop", label: "自主实现", description: "自行控制模型、动作与反馈循环", threshold: 0.8 }],
  evidence_kinds: ["metadata"], revision: 1, created_at: "2026-10-31", updated_at: "2026-10-31",
};
const job: Job = {
  id: "job-fixture", policy_id: policy.id, policy_name: policy.name, policy_revision: 1,
  status: "queued", total: 2, completed: 0, failed: 0, skipped: 0, cost: 0, unknown_cost_count: 0,
  created_at: "2026-10-31", updated_at: "2026-10-31", error: null,
};
const generation: GeneratedPolicy = {
  message: "已按应用场景填写，可继续修改。", model: "glm-5.3", usage: {}, cost: null, generation_id: "g-fixture",
  policy: { name: "应用场景", description: "依据README和元数据判断", mode: "multi_label", threshold: .5, evidence_kinds: ["metadata", "readme"],
    tags: [{ id: "coding", label: "Coding Agent", description: "Develops and modifies software.", threshold: .5 }] },
};
const glmHealth = { status: "ok", jev_configured: true, glm_configured: true, model: "typesafe/jev-1.14" };

let previousScrollIntoView: typeof Element.prototype.scrollIntoView;
beforeEach(() => {
  vi.resetAllMocks();
  // jsdom 不计算布局；这里仅补齐 Radix Checkbox 所需的浏览器接口。
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  previousScrollIntoView = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); Element.prototype.scrollIntoView = previousScrollIntoView; });

describe("策略编辑", () => {
  it("初始不保存或预填策略；用户填写后才提交完整规则", async () => {
    const saved = vi.fn();
    vi.mocked(api.savePolicy).mockResolvedValue(policy);
    render(<PolicyEditor open policy={null} onOpenChange={vi.fn()} onSaved={saved} />);
    expect((screen.getByLabelText(/策略名称/) as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("标签 1 名称") as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("combobox", { name: "数据来源" })).toBeNull();
    expect(api.savePolicy).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/策略名称/), { target: { value: "实现形式" } });
    fireEvent.change(screen.getByLabelText("标签 1 名称"), { target: { value: "自主实现" } });
    fireEvent.change(screen.getByLabelText("标签 1 判断规则"), { target: { value: "自行控制模型、动作与反馈循环" } });
    fireEvent.change(screen.getByLabelText("标签 1 阈值"), { target: { value: "0.72" } });
    fireEvent.click(screen.getByRole("button", { name: "保存策略" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(policy));
    expect(api.savePolicy).toHaveBeenCalledTimes(1);
    const [submitted] = vi.mocked(api.savePolicy).mock.calls[0];
    expect(submitted.name).toBe("实现形式");
    expect(submitted.tags[0].description).toBe("自行控制模型、动作与反馈循环");
    expect(submitted.tags[0].threshold).toBe(0.72);
    expect(submitted.evidence_kinds).toEqual(["metadata", "readme"]);
  });

  it("编辑失败显示错误并保留用户内容", async () => {
    vi.mocked(api.savePolicy).mockRejectedValue(new Error("策略保存失败，请重试"));
    render(<PolicyEditor open policy={policy} onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "保存策略" }));
    expect(await screen.findByText("策略保存失败，请重试")).toBeTruthy();
    expect((screen.getByLabelText("标签 1 名称") as HTMLInputElement).value).toBe("自主实现");
  });

  it("没有模板入口，清空只重置当前草稿", () => {
    render(<PolicyEditor open policy={null} onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.queryByRole("combobox", { name: "策略模板" })).toBeNull();
    fireEvent.change(screen.getByLabelText(/策略名称/), { target: { value: "自定义策略" } });
    fireEvent.change(screen.getByLabelText("标签 1 名称"), { target: { value: "自定义标签" } });
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect((screen.getByLabelText(/策略名称/) as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("标签 1 名称") as HTMLInputElement).value).toBe("");
    expect(api.savePolicy).not.toHaveBeenCalled();
    expect(api.createJob).not.toHaveBeenCalled();
  });

  it("示例只填入需求，明确生成后填写草稿并支持继续对话", async () => {
    vi.mocked(api.generatePolicy).mockResolvedValue(generation);
    render(<PolicyEditor open policy={null} health={glmHealth} onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "填入示例" }));
    expect((screen.getByLabelText("自然语言策略需求") as HTMLTextAreaElement).value).toContain("Penetration Testing Agent");
    expect(api.generatePolicy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "生成策略" }));
    await waitFor(() => expect((screen.getByLabelText(/策略名称/) as HTMLInputElement).value).toBe("应用场景"));
    expect((screen.getByLabelText("标签 1 名称") as HTMLInputElement).value).toBe("Coding Agent");
    fireEvent.change(screen.getByLabelText("自然语言策略需求"), { target: { value: "再增加网页浏览标签" } });
    fireEvent.click(screen.getByRole("button", { name: "生成策略" }));
    await waitFor(() => expect(api.generatePolicy).toHaveBeenCalledTimes(2));
    const [messages, current] = vi.mocked(api.generatePolicy).mock.calls[1];
    expect(messages.map(message => message.role)).toEqual(["user", "assistant", "user"]);
    expect(current.tags[0].label).toBe("Coding Agent");
    expect(api.savePolicy).not.toHaveBeenCalled();
    expect(api.createJob).not.toHaveBeenCalled();
  });

  it("清空后忽略尚未返回的生成内容，失败也不覆盖已有草稿", async () => {
    let resolve!: (value: GeneratedPolicy) => void;
    vi.mocked(api.generatePolicy).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    render(<PolicyEditor open policy={null} health={glmHealth} onOpenChange={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("自然语言策略需求"), { target: { value: "生成用途分类" } });
    fireEvent.click(screen.getByRole("button", { name: "生成策略" }));
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    resolve(generation);
    await waitFor(() => expect((screen.getByLabelText(/策略名称/) as HTMLInputElement).value).toBe(""));
    vi.mocked(api.generatePolicy).mockRejectedValueOnce(new Error("GLM 返回格式无效"));
    fireEvent.change(screen.getByLabelText(/策略名称/), { target: { value: "保留当前草稿" } });
    fireEvent.change(screen.getByLabelText("自然语言策略需求"), { target: { value: "生成用途分类" } });
    fireEvent.click(screen.getByRole("button", { name: "生成策略" }));
    await screen.findByText("GLM 返回格式无效");
    expect((screen.getByLabelText(/策略名称/) as HTMLInputElement).value).toBe("保留当前草稿");
    expect(api.savePolicy).not.toHaveBeenCalled();
  });
});

describe("启动分类", () => {
  const context = { selectedIds: [1, 2], filters: { ...emptyFilters }, total: 20 };
  const health = { status: "ok", jev_configured: true, model: "typesafe/jev-1.14" };

  it("打开任务表单不发请求，明确点击后只提交一次选中范围", async () => {
    const created = vi.fn();
    vi.mocked(api.createJob).mockResolvedValue(job);
    render(<RunDialog context={context} policies={[policy]} health={health} onClose={vi.fn()} onCreated={created} onOpenPolicies={vi.fn()} />);
    expect(api.createJob).not.toHaveBeenCalled();
    const submit = screen.getByRole("button", { name: "开始分类" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(created).toHaveBeenCalledWith(job));
    expect(api.createJob).toHaveBeenCalledTimes(1);
    expect(api.createJob).toHaveBeenCalledWith(policy.id, { repository_ids: [1, 2], limit: 2 }, true, "typesafe/jev-1.14", ["metadata", "readme"]);
  });

  it("凭证未配置时阻止任务提交", () => {
    render(<RunDialog context={context} policies={[policy]} health={{ ...health, jev_configured: false }} onClose={vi.fn()} onCreated={vi.fn()} onOpenPolicies={vi.fn()} />);
    const submit = screen.getByRole("button", { name: "开始分类" });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(submit);
    expect(api.createJob).not.toHaveBeenCalled();
  });

  it("没有已保存策略时先前往策略页面，不启动任务或嵌套编辑", () => {
    const openPolicies = vi.fn();
    render(<RunDialog context={context} policies={[]} health={health} onClose={vi.fn()} onCreated={vi.fn()} onOpenPolicies={openPolicies} />);
    expect(screen.queryByRole("button", { name: "新建策略" })).toBeNull();
    expect(screen.getByRole("button", { name: "开始分类" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "前往分类策略" }));
    expect(openPolicies).toHaveBeenCalledTimes(1);
    expect(api.createJob).not.toHaveBeenCalled();
    expect(api.savePolicy).not.toHaveBeenCalled();
  });

  it("任务选择仅包含已保存策略，名称不附带版本且选中不切换表单", async () => {
    const openPolicies = vi.fn();
    render(<RunDialog context={context} policies={[policy]} health={health} onClose={vi.fn()} onCreated={vi.fn()} onOpenPolicies={openPolicies} />);
    fireEvent.keyDown(screen.getByRole("combobox", { name: "选择分类策略" }), { key: "Enter" });
    const option = await screen.findByRole("option", { name: policy.name });
    expect(screen.queryByRole("option", { name: /模板|v1/ })).toBeNull();
    fireEvent.keyDown(option, { key: "Enter" });
    expect(screen.getByRole("heading", { name: "新建分类任务" })).toBeTruthy();
    expect(openPolicies).not.toHaveBeenCalled();
    expect(api.createJob).not.toHaveBeenCalled();
  });

  it("GLM配置独立于Jev，选择的模型随任务提交", async () => {
    vi.mocked(api.createJob).mockResolvedValue(job);
    render(<RunDialog context={context} policies={[policy]} health={{ ...glmHealth, jev_configured: false }} onClose={vi.fn()} onCreated={vi.fn()} onOpenPolicies={vi.fn()} />);
    fireEvent.keyDown(screen.getByRole("combobox", { name: "分类模型" }), { key: "Enter" });
    fireEvent.keyDown(await screen.findByRole("option", { name: "GLM 5.3" }), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "开始分类" }));
    await waitFor(() => expect(api.createJob).toHaveBeenCalledWith(policy.id, { repository_ids: [1, 2], limit: 2 }, true, "glm-5.3", ["metadata", "readme"]));
  });

  it("完整仓库来源禁用，焦点提示等待实现中", async () => {
    render(<RunDialog context={context} policies={[]} health={health} onClose={vi.fn()} onCreated={vi.fn()} onOpenPolicies={vi.fn()} />);
    fireEvent.keyDown(screen.getByRole("combobox", { name: "数据来源" }), { key: "Enter" });
    const wholeRepository = await screen.findByRole("option", { name: "整个仓库（含代码）" });
    expect(wholeRepository.getAttribute("aria-disabled")).toBe("true");
    fireEvent.focus(screen.getByLabelText("整个仓库（含代码）：等待实现中"));
    expect(await screen.findByRole("tooltip")).toHaveProperty("textContent", "等待实现中");
    expect(api.createJob).not.toHaveBeenCalled();
  });
});

describe("人工标签", () => {
  const annotationPolicy = {
    policy_id: policy.id, policy_name: "用途", policy_revision: 3, mode: "multi_label" as const,
    tags: [
      { tag_id: "coding", label: "代码开发", description: "生成与修改代码", model_score: .9, model_applied: true, manual_applied: null, score: .9, applied: true },
      { tag_id: "browser", label: "网页操作", description: "操作网页", model_score: .2, model_applied: false, manual_applied: null, score: .2, applied: false },
    ],
  };

  it("编辑后才保存人工决定，重复点击不会重复提交", async () => {
    vi.mocked(api.repositoryAnnotations).mockResolvedValue({ items: [annotationPolicy] });
    const saved = vi.fn();
    render(<ManualTagEditor repositoryId={1} onCancel={vi.fn()} onSaved={saved} />);
    const browser = await screen.findByRole("checkbox", { name: "网页操作" });
    expect(api.saveRepositoryAnnotations).not.toHaveBeenCalled();
    fireEvent.click(browser);
    expect(screen.getByText("人工 · 1.00")).toBeTruthy();
    const submit = screen.getByRole("button", { name: "保存标签" });
    fireEvent.click(submit); fireEvent.click(submit);
    await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
    expect(api.saveRepositoryAnnotations).toHaveBeenCalledExactlyOnceWith(1, policy.id, 3, [{ tag_id: "browser", applied: true }]);
    expect(api.createJob).not.toHaveBeenCalled();
    expect(api.generatePolicy).not.toHaveBeenCalled();
  });

  it("单标签人工选择替换原模型标签", async () => {
    vi.mocked(api.repositoryAnnotations).mockResolvedValue({ items: [{ ...annotationPolicy, mode: "single_label" }] });
    render(<ManualTagEditor repositoryId={1} onCancel={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "网页操作" }));
    expect(screen.getByRole("checkbox", { name: "代码开发" }).getAttribute("aria-checked")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "保存标签" }));
    await waitFor(() => expect(api.saveRepositoryAnnotations).toHaveBeenCalledWith(1, policy.id, 3, [
      { tag_id: "browser", applied: true },
    ]));
  });

  it("恢复自动结果仍需明确保存，提交空覆盖列表", async () => {
    vi.mocked(api.repositoryAnnotations).mockResolvedValue({ items: [{ ...annotationPolicy,
      tags: annotationPolicy.tags.map(tag => tag.tag_id === "browser" ? { ...tag, manual_applied: true, score: 1, applied: true } : tag),
    }] });
    render(<ManualTagEditor repositoryId={1} onCancel={vi.fn()} onSaved={vi.fn()} />);
    await screen.findByText("人工 · 1.00");
    fireEvent.click(screen.getByRole("button", { name: /^恢复自动结果$/ }));
    expect(api.saveRepositoryAnnotations).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "网页操作" }).getAttribute("aria-checked")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "保存标签" }));
    await waitFor(() => expect(api.saveRepositoryAnnotations).toHaveBeenCalledWith(1, policy.id, 3, []));
  });
});
