import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type RepositoryFilters } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("saved-score API calls", () => {
  it("serializes multiple selected tags in GET while preserving task JSON arrays", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const filters: RepositoryFilters = { q: "agent", language: "Python", min_stars: 1000, sort: "stars", policy_id: "p1", tag_id: "legacy", tag_ids: ["code", "browser"] };
    await api.repositories(filters, 2, 25);
    const url = new URL(fetch.mock.calls[0][0], "http://localhost");
    expect(url.searchParams.get("tag_ids")).toBe("code,browser");
    expect(url.searchParams.has("tag_id")).toBe(false);
    expect(url.searchParams.get("language")).toBe("Python");
    fetch.mockResolvedValue(new Response(JSON.stringify({ id: "job" }), { status: 200 }));
    await api.createJob("p1", { filters, limit: 100 }, true);
    expect(JSON.parse(fetch.mock.calls[1][1].body).scope.filters.tag_ids).toEqual(["code", "browser"]);
  });

  it("sends the task data source separately from the saved policy", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "job" }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await api.createJob("p1", { repository_ids: [1], limit: 1 }, true, "glm-5.3", ["metadata", "readme"]);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      policy_id: "p1", scope: { repository_ids: [1], limit: 1 }, skip_completed: true,
      model: "glm-5.3", evidence_kinds: ["metadata", "readme"],
    });
  });

  it("saves threshold revisions without starting a classification job", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "p1", revision: 5 }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const thresholds = { code: 0.73, research: 0.92 };
    await api.applyThresholds("p1", 4, thresholds);
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, options] = fetch.mock.calls[0];
    expect(url).toBe("/api/policies/p1/thresholds");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ base_revision: 4, thresholds });
    expect(fetch.mock.calls.some(([path]) => String(path).includes("/jobs"))).toBe(false);
  });

  it("restores a historical policy through the restore endpoint only", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "p1", revision: 6 }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await api.restorePolicy("p1", 2);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe("/api/policies/p1/restore/2");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
  });

  it("keeps revision and paging explicit when browsing preserved results", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await api.policyResults("p1", 2, "研究", 3);
    const url = new URL(fetch.mock.calls[0][0], "http://localhost");
    expect(url.pathname).toBe("/api/policies/p1/results");
    expect(Object.fromEntries(url.searchParams)).toEqual({ revision: "2", q: "研究", page: "3", page_size: "20" });
  });

  it("reports a stale revision without automatically retrying or creating jobs", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "策略版本已更新，请刷新" }), { status: 409 }));
    vi.stubGlobal("fetch", fetch);
    await expect(api.applyThresholds("p1", 1, { code: 0.8 })).rejects.toMatchObject({ status: 409, message: "策略版本已更新，请刷新" } satisfies Partial<ApiError>);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
