import { describe, expect, it } from "vitest";
import type { JobStatus } from "@/lib/api";
import { formatJobDuration } from "./jobs";

const start = "2026-11-04T04:00:00Z";
const later = "2026-11-04T04:01:02Z";

describe("分类任务耗时", () => {
  it("终态使用保存的结束时间，不随当前时间继续增加", () => {
    for (const status of ["completed", "completed_with_errors", "failed", "cancelled"] as JobStatus[]) {
      expect(formatJobDuration({ status, created_at: start, updated_at: later }, Date.parse("2026-11-05T04:00:00Z"))).toBe("1分 2秒");
    }
  });

  it("未结束任务按当前时间计算，包含排队和暂停", () => {
    for (const status of ["queued", "running", "paused"] as JobStatus[]) {
      const job = { status, created_at: start, updated_at: later };
      expect(formatJobDuration(job, Date.parse(later))).toBe("1分 2秒");
      expect(formatJobDuration(job, Date.parse("2026-11-04T05:02:03Z"))).toBe("1时 2分 3秒");
    }
  });

  it("缺失时间显示占位，时钟偏差不产生负耗时", () => {
    expect(formatJobDuration({ status: "completed", created_at: start, updated_at: "" })).toBe("—");
    expect(formatJobDuration({ status: "running", created_at: start, updated_at: later }, Date.parse(start) - 5000)).toBe("0秒");
  });
});
