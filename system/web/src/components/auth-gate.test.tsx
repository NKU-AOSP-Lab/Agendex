// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AuthGate } from "./auth-gate";
import { authApi, type AuthSession } from "@/lib/auth";
import { request, SESSION_EXPIRED_EVENT } from "@/lib/api";

vi.mock("@/lib/auth", () => ({ authApi: { session: vi.fn(), login: vi.fn(), logout: vi.fn() } }));

const signedIn: AuthSession = { enabled: true, authenticated: true, username: "fixture-user" };
const signedOut: AuthSession = { enabled: true, authenticated: false, username: null };
const workbench = vi.fn((_session: AuthSession, logout: () => Promise<void>) => <div>私有目录<button onClick={() => void logout()}>退出登录</button></div>);

beforeEach(() => { vi.resetAllMocks(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("登录访问", () => {
  it("确认会话前不挂载工作台，未启用登录的本地模式保持可用", async () => {
    let resolve!: (value: AuthSession) => void;
    vi.mocked(authApi.session).mockImplementation(() => new Promise(done => { resolve = done; }));
    render(<AuthGate>{workbench}</AuthGate>);
    expect(workbench).not.toHaveBeenCalled();
    expect(screen.queryByText("私有目录")).toBeNull();
    resolve({ enabled: false, authenticated: true, username: null });
    expect(await screen.findByText("私有目录")).toBeTruthy();
    expect(authApi.login).not.toHaveBeenCalled();
  });

  it("明确提交才登录且重复点击只发一次，退出后回到登录页", async () => {
    vi.mocked(authApi.session).mockResolvedValue(signedOut);
    vi.mocked(authApi.login).mockResolvedValue(signedIn);
    vi.mocked(authApi.logout).mockResolvedValue(signedOut);
    render(<AuthGate>{workbench}</AuthGate>);
    fireEvent.change(await screen.findByLabelText("账号"), { target: { value: "fixture-user" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "fixture-password" } });
    expect(authApi.login).not.toHaveBeenCalled();
    const submit = screen.getByRole("button", { name: "登录" });
    fireEvent.click(submit); fireEvent.click(submit);
    expect(await screen.findByText("私有目录")).toBeTruthy();
    expect(authApi.login).toHaveBeenCalledExactlyOnceWith("fixture-user", "fixture-password");
    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
    await screen.findByRole("button", { name: "登录" });
    expect((screen.getByLabelText("密码") as HTMLInputElement).value).toBe("");
    expect(screen.queryByText("私有目录")).toBeNull();
  });

  it("错误密码不显示受保护内容，且允许用户重试", async () => {
    vi.mocked(authApi.session).mockResolvedValue(signedOut);
    vi.mocked(authApi.login).mockRejectedValue(new Error("账号或密码不正确"));
    render(<AuthGate>{workbench}</AuthGate>);
    fireEvent.change(await screen.findByLabelText("账号"), { target: { value: "fixture-user" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "incorrect" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "账号或密码不正确");
    expect(workbench).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "登录" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("接口会话失效时卸载私有页面并要求重新登录", async () => {
    vi.mocked(authApi.session).mockResolvedValue(signedIn);
    render(<AuthGate>{workbench}</AuthGate>);
    await screen.findByText("私有目录");
    fireEvent(window, new Event(SESSION_EXPIRED_EVENT));
    await screen.findByText("登录已过期，请重新登录。");
    expect(screen.queryByText("私有目录")).toBeNull();
    expect(screen.getByLabelText("密码")).toBeTruthy();
    await waitFor(() => expect(authApi.session).toHaveBeenCalledTimes(1));
  });

  it("私有接口401通知登录门禁，登录失败401仅反馈给当前表单", async () => {
    const expired = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    try {
      const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ detail: "需要登录" }), { status: 401 }));
      vi.stubGlobal("fetch", fetch);
      await expect(request("/stats")).rejects.toMatchObject({ status: 401 });
      expect(expired).toHaveBeenCalledTimes(1);
      await expect(request("/auth/login", { method: "POST" })).rejects.toMatchObject({ status: 401 });
      expect(expired).toHaveBeenCalledTimes(1);
    } finally { window.removeEventListener(SESSION_EXPIRED_EVENT, expired); }
  });
});
