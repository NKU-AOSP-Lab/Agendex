import { useEffect, useState } from "react";
import { Check, CirclePause, Clock3, ExternalLink, ListTodo, Loader2, Pause, Play, Square, X } from "lucide-react";
import { api, type Job, type JobStatus } from "@/lib/api";
import { jobProgress } from "@/lib/domain";
import { classifierLabel } from "@/lib/classifiers";
import { formatDate, formatNumber } from "@/lib/utils";
import { useResource } from "@/hooks/use-resource";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { evidenceSourceLabel } from "@/components/evidence-source-select";
import { EmptyState, ErrorState, InlineError, Loading } from "@/components/shared";

export const statusLabels: Record<JobStatus, string> = { queued: "排队中", running: "进行中", paused: "已暂停", completed: "已完成", completed_with_errors: "部分失败", failed: "失败", cancelled: "已取消" };

/** Wall-clock duration includes queueing and pauses; finished jobs keep their end time. */
export function formatJobDuration(job: Pick<Job, "status" | "created_at" | "updated_at">, currentTime = Date.now()) {
  const start = Date.parse(job.created_at);
  const ended = ["completed", "completed_with_errors", "failed", "cancelled"].includes(job.status);
  const end = ended ? Date.parse(job.updated_at) : currentTime;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
  const totalSeconds = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const seconds = totalSeconds % 60;
  return hours ? `${hours}时 ${minutes}分 ${seconds}秒` : minutes ? `${minutes}分 ${seconds}秒` : `${seconds}秒`;
}

export function JobsPage({ refresh, onChanged, onRepository }: { refresh: number; onChanged: () => void; onRepository: (id: number) => void }) {
  const [poll, setPoll] = useState(0);
  const resource = useResource(signal => api.jobs(signal), [refresh, poll]);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [cancelJob, setCancelJob] = useState<Job | null>(null);
  const [currentTime, setCurrentTime] = useState(Date.now);
  const jobs = resource.data?.items || [];
  const hasOpenJobs = jobs.some(job => ["queued", "running", "paused"].includes(job.status));
  useEffect(() => {
    if (!hasOpenJobs) return;
    setCurrentTime(Date.now());
    // This updates the local clock only; it does not add API polling or model calls.
    const timer = window.setInterval(() => setCurrentTime(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasOpenJobs]);
  useEffect(() => { const timer = window.setInterval(() => setPoll(value => value + 1), 4000); return () => clearInterval(timer); }, []);
  async function action(job: Job, command: "pause" | "resume" | "cancel") {
    setPending(job.id); setError(null);
    try { await api.jobAction(job.id, command); setCancelJob(null); resource.reload(); onChanged(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setPending(null); }
  }
  return <>
    <div className="page-heading"><div><h1>分类任务</h1></div><span className="live-indicator"><i />自动更新</span></div>
    <InlineError message={error} />
    {resource.error ? <ErrorState message={resource.error} onRetry={resource.reload} /> : !resource.data && resource.loading ? <Loading /> : !jobs.length ? <div className="empty-policy-panel"><EmptyState title="还没有分类任务" description="在仓库页选择范围，使用你的策略开始分类。" icon={<ListTodo size={27} strokeWidth={1.5} />} /></div> : <div className="jobs-list">{jobs.map(job => <article className="job-card" key={job.id}>
      <div className="job-card-top">
        <div className="job-heading">
          <div className="job-title-line">
            <button className="job-title" title={job.policy_name} onClick={() => setDetailId(job.id)}>{job.policy_name}<span>v{job.policy_revision}</span></button>
            <JobStatusBadge status={job.status} />
          </div>
          <p>{formatDate(job.created_at, true)}<span>·</span>{formatNumber(job.total)} 个仓库{job.model && <><span>·</span>{classifierLabel(job.model)}</>}</p>
        </div>
        <div className="job-actions">
          <Button variant="ghost" size="sm" onClick={() => setDetailId(job.id)}>详情<ExternalLink size={12} /></Button>
          {(job.status === "queued" || job.status === "running") && <Button variant="outline" size="sm" disabled={pending === job.id} onClick={() => action(job, "pause")}>{pending === job.id ? <Loader2 size={13} className="spin" /> : <Pause size={13} />}暂停</Button>}
          {job.status === "paused" && <Button variant="outline" size="sm" disabled={pending === job.id} onClick={() => action(job, "resume")}>{pending === job.id ? <Loader2 size={13} className="spin" /> : <Play size={13} />}继续</Button>}
          {["queued", "running", "paused"].includes(job.status) && <Button variant="ghost" size="icon" aria-label={`取消 ${job.policy_name} 任务`} disabled={pending === job.id} onClick={() => setCancelJob(job)}><Square size={13} /></Button>}
        </div>
      </div>
      <div className="job-card-bottom">
        <div className="job-progress"><Progress value={jobProgress(job)} label={`${job.policy_name} 分类进度`} /><span>{Math.round(jobProgress(job))}%</span></div>
        <div className="job-counts">
          <span><Check size={13} />{formatNumber(job.completed)} 完成</span>
          {job.failed > 0 && <span className="text-danger"><X size={13} />{formatNumber(job.failed)} 失败</span>}
          {job.skipped > 0 && <span>{formatNumber(job.skipped)} 复用</span>}
          <span className="job-cost">费用 {formatCost(job)}</span>
          <span className="job-duration" title="从任务创建到结束的总时长，包含排队和暂停时间"><Clock3 size={12} />耗时 {formatJobDuration(job, currentTime)}</span>
        </div>
      </div>
      {job.error && <p className="job-error">{job.error}</p>}
    </article>)}</div>}
    <JobDetailDrawer id={detailId} onClose={() => setDetailId(null)} refresh={poll + refresh} onRepository={id => { setDetailId(null); onRepository(id); }} />
    <Dialog open={Boolean(cancelJob)} onOpenChange={open => { if (!open && !pending) setCancelJob(null); }}><DialogContent className="small-dialog"><div className="dialog-heading"><DialogTitle>取消分类任务？</DialogTitle><DialogDescription>已完成的结果会保留，尚未开始的仓库将不再处理。</DialogDescription></div><div className="dialog-footer"><span /><div><Button variant="outline" disabled={Boolean(pending)} onClick={() => setCancelJob(null)}>返回</Button><Button disabled={Boolean(pending)} onClick={() => cancelJob && action(cancelJob, "cancel")}>{pending && <Loader2 size={14} className="spin" />}取消任务</Button></div></div></DialogContent></Dialog>
  </>;
}

function formatCost(job: Job) {
  if (job.cost == null) return "未知";
  return `$${job.cost.toFixed(4)}${job.unknown_cost_count > 0 ? " + 未知" : ""}`;
}

function JobStatusBadge({ status }: { status: JobStatus }) {
  const Icon = status === "running" ? Loader2 : status === "queued" ? Clock3 : status === "paused" ? CirclePause : status === "completed" ? Check : X;
  return <span className={`job-status status-${status}`}><Icon size={13} className={status === "running" ? "spin" : ""} />{statusLabels[status] || status}</span>;
}

function JobDetailDrawer({ id, onClose, refresh, onRepository }: { id: string | null; onClose: () => void; refresh: number; onRepository: (id: number) => void }) {
  const resource = useResource(signal => id ? api.job(id, signal) : Promise.resolve(undefined), [id, refresh]);
  const job = resource.data?.id === id ? resource.data : undefined;
  const itemLabels: Record<string, string> = { queued: "待处理", running: "处理中", completed: "已完成", failed: "失败", skipped: "已复用", cancelled: "已取消" };
  return <Dialog open={Boolean(id)} onOpenChange={open => { if (!open) onClose(); }}><DialogContent drawer><div className="drawer-heading"><DialogTitle>{job?.policy_name || "任务详情"}</DialogTitle><DialogDescription className="sr-only">分类任务的状态与仓库处理记录</DialogDescription></div>{resource.error ? <ErrorState message={resource.error} onRetry={resource.reload} /> : !job ? <Loading /> : <div className="drawer-body"><div className="job-detail-summary"><JobStatusBadge status={job.status} /><span>v{job.policy_revision} · {formatDate(job.created_at, true)}</span></div><dl className="detail-facts">{job.model && <div><dt>分类模型</dt><dd>{classifierLabel(job.model)}</dd></div>}{job.evidence_kinds && <div><dt>数据来源</dt><dd>{evidenceSourceLabel(job.evidence_kinds)}</dd></div>}<div><dt>总仓库数</dt><dd>{formatNumber(job.total)}</dd></div><div><dt>已完成</dt><dd>{formatNumber(job.completed)}</dd></div><div><dt>失败</dt><dd>{formatNumber(job.failed)}</dd></div><div><dt>费用</dt><dd>{formatCost(job)}</dd></div><div><dt>总耗时</dt><dd title="包含排队和暂停时间">{formatJobDuration(job)}</dd></div></dl>{job.error && <InlineError message={job.error} />}<section className="detail-section"><h3>最近处理记录</h3>{job.items?.length ? <div className="job-item-list">{job.items.map(item => <div key={item.id}><button onClick={() => onRepository(item.repository_id)}>{item.full_name || `仓库 #${item.repository_id}`}</button><span className={item.status === "failed" ? "text-danger" : ""}>{itemLabels[item.status] || item.status}</span>{item.error && <p>{item.error}</p>}</div>)}</div> : <p className="detail-empty">暂无处理记录</p>}</section></div>}</DialogContent></Dialog>;
}
