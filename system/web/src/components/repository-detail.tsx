import { useEffect, useState } from "react";
import { ArrowUpRight, Check, GitBranch, Pencil, Star } from "lucide-react";
import { api, type Evidence } from "@/lib/api";
import { formatDate, formatNumber } from "@/lib/utils";
import { useResource } from "@/hooks/use-resource";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";
import { ErrorState, Loading } from "./shared";
import { ManualTagEditor } from "./manual-tag-editor";

export function RepositoryDetailDrawer({ id, onClose, refresh, onChanged }: { id: number | null; onClose: () => void; refresh: number; onChanged: () => void }) {
  const [editingTags, setEditingTags] = useState(false);
  useEffect(() => setEditingTags(false), [id]);
  const resource = useResource(signal => id === null ? Promise.resolve(undefined) : api.repository(id, signal), [id, refresh]);
  // Do not flash a previously opened repository while a new detail request runs.
  const repository = resource.data?.id === id ? resource.data : undefined;
  const readmes = repository?.evidence.filter(evidence => evidence.kind === "readme") || [];
  return <Dialog open={id !== null} onOpenChange={open => { if (!open) onClose(); }}><DialogContent drawer className="repository-drawer">
    <div className="drawer-heading"><DialogTitle>{repository?.full_name || "仓库详情"}</DialogTitle><DialogDescription className="sr-only">仓库信息、README与分类标签</DialogDescription></div>
    {resource.error ? <ErrorState message={resource.error} onRetry={resource.reload} /> : resource.loading && !repository ? <Loading /> : repository && <div className="drawer-body">
      <p className="detail-description">{repository.description || "暂无描述"}</p>
      <div className="detail-primary-actions"><Button variant="outline" size="sm" asChild><a href={repository.url} target="_blank" rel="noopener noreferrer">GitHub<ArrowUpRight size={14} /></a></Button><span><Star size={14} />{formatNumber(repository.stars)}</span>{repository.default_branch && <span><GitBranch size={14} />{repository.default_branch}</span>}</div>
      <dl className="detail-facts"><div><dt>语言</dt><dd>{repository.language || "—"}</dd></div><div><dt>许可证</dt><dd>{repository.license || "—"}</dd></div><div><dt>创建时间</dt><dd>{formatDate(repository.created_at)}</dd></div><div><dt>最近推送</dt><dd>{formatDate(repository.pushed_at)}</dd></div></dl>
      <section className="detail-section"><div className="detail-section-heading"><h3>分类标签</h3>{!editingTags && <Button variant="ghost" size="sm" onClick={() => setEditingTags(true)}><Pencil size={12} />编辑标签</Button>}</div>{editingTags ? <ManualTagEditor key={repository.id} repositoryId={repository.id} onCancel={() => setEditingTags(false)} onSaved={() => { setEditingTags(false); resource.reload(); onChanged(); }} /> : repository.tags?.length ? <div className="tag-list">{repository.tags.map(tag => <span className="tag" key={`${tag.policy_id}-${tag.tag_id}`} title={`${tag.source === "manual" ? "人工标注" : "模型分类"} · 分数 ${tag.score.toFixed(2)}`}>{tag.label}{tag.source === "manual" && <small className="manual-tag-mark">人工 · 1.00</small>}</span>)}</div> : <p className="detail-empty">尚未打标签</p>}</section>
      {repository.topics?.length > 0 && <section className="detail-section"><details className="plain-details"><summary>GitHub Topics <span>{repository.topics.length}</span></summary><p className="topic-text">{repository.topics.join(" · ")}</p></details></section>}
      {readmes.length > 0 && <section className="detail-section"><h3>README</h3>{readmes.map(evidence => <ReadmeCard key={evidence.id} evidence={evidence} />)}</section>}
      <section className="detail-section"><h3>分类记录</h3>{repository.classifications.length ? <div className="classification-history">{repository.classifications.map((classification, index) => <details className="classification-record" key={`${classification.job_id}-${index}`}>
        <summary><span>{classification.policy_name}<small>v{classification.policy_revision}</small></span><time>{formatDate(classification.created_at, true)}</time></summary>
        <div className="classification-result-list">{classification.results.map(result => <div key={result.tag_id}><span className={result.applied ? "" : "muted"}>{result.applied && <Check size={13} />}{result.label}</span><span>{result.score?.toFixed(2) ?? "—"}</span></div>)}</div><div className="record-provenance">{classification.model} · {classification.evidence_ids.length} 份证据</div>
      </details>)}</div> : <p className="detail-empty">暂无分类记录</p>}</section>
    </div>}
  </DialogContent></Dialog>;
}

function ReadmeCard({ evidence }: { evidence: Evidence }) {
  return <details className="plain-details"><summary>展开文档</summary>
    <p className="readme-preview">{typeof evidence.content === "string" ? evidence.content : ""}</p>
    {evidence.source_url && <div className="detail-primary-actions"><a href={evidence.source_url} target="_blank" rel="noopener noreferrer">在 GitHub 阅读<ArrowUpRight size={12} /></a></div>}
  </details>;
}
