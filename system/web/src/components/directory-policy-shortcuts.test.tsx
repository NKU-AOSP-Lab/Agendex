// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Policy } from "@/lib/api";
import { DirectoryPolicyShortcuts } from "./directory-policy-shortcuts";

const policies: Policy[] = Array.from({ length: 10 }, (_, index) => ({
  id: `p${index + 1}`, name: `策略 ${index + 1}`, description: "", mode: "multi_label", threshold: .5,
  tags: [], evidence_kinds: ["metadata", "readme"], revision: 1, created_at: "", updated_at: "",
}));
function renderShortcuts() {
  return render(<DirectoryPolicyShortcuts policies={policies} selectedPolicyId="" selectedTagIds={[]}
    onSelectPolicy={vi.fn()} onSelectTag={vi.fn()} onCreatePolicy={vi.fn()} />);
}

let originalStorage: PropertyDescriptor | undefined;
let originalScroll: typeof Element.prototype.scrollIntoView;
beforeEach(() => {
  // Isolate browser preferences from Node's own experimental storage implementation.
  originalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", { configurable: true, value: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  } });
  originalScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  if (originalStorage) Object.defineProperty(window, "localStorage", originalStorage);
  Element.prototype.scrollIntoView = originalScroll;
});

describe("常用策略", () => {
  it("可选择超过4个常用策略，并在重新打开后保留完整选择", async () => {
    window.localStorage.setItem("agendex.directory.favorite-policies", JSON.stringify(["p1", "p2", "p3", "p4"]));
    renderShortcuts();
    expect(screen.getByRole("button", { name: "策略 1" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "策略 5" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("button", { name: "管理常用" }), { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: "策略 5" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "策略 6" }));
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem("agendex.directory.favorite-policies")!)).toEqual(["p1", "p2", "p3", "p4", "p5", "p6"]));
    cleanup(); renderShortcuts();
    expect(screen.getByRole("button", { name: "策略 5" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "策略 6" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "策略 7" })).toBeNull();
  });

  it("明确取消所有常用后，不会在重新打开时恢复默认项", () => {
    window.localStorage.setItem("agendex.directory.favorite-policies", "[]");
    renderShortcuts();
    expect(screen.getByText("暂未设置常用策略")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "策略 1" })).toBeNull();
    expect(screen.getByRole("button", { name: "管理常用" })).toBeTruthy();
  });
});
