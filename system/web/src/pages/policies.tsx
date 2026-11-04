import { useState } from "react";
import { ArrowRight, Braces, ListFilter, Loader2, Plus, Tags } from "lucide-react";
import { api, type Policy } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ActionMenu } from "@/components/ui/dropdown-menu";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, ErrorState, InlineError, Loading } from "@/components/shared";
import { formatDate, formatNumber } from "@/lib/utils";

export function PoliciesPage({ policies, loading, error, onReload, onCreate, onEdit, onRun, onResults, onDeleted }: {
  policies: Policy[]; loading: boolean; error: string | null; onReload: () => void;
  onCreate: () => void; onEdit: (policy: Policy) => void; onRun: (policy: Policy) => void; onResults: (policy: Policy) => void; onDeleted: () => void;
}) {
  const [deleting, setDeleting] = useState<Policy | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  async function remove() {
    if (!deleting) return;
    setBusy(true); setDeleteError(null);
    try { await api.deletePolicy(deleting.id); setDeleting(null); onDeleted(); }
    catch (reason) { setDeleteError(reason instanceof Error ? reason.message : "删除失败"); }
    finally { setBusy(false); }
  }
  return <>
    <div className="page-heading"><div><h1>分类策略</h1></div><Button onClick={onCreate}><Plus size={16} />新建策略</Button></div>
    {error ? <ErrorState message={error} onRetry={onReload} /> : loading && !policies.length ? <Loading /> : !policies.length ? <div className="empty-policy-panel"><EmptyState title="创建第一条分类策略" description="添加标签与判断规则，保存后用于分类任务。" icon={<Tags size={26} strokeWidth={1.4} />}><Button onClick={onCreate}><Plus size={15} />新建策略</Button></EmptyState><div className="policy-empty-footer"><Braces size={15} /><span>自定义分类规则，保存并复用分类结果。</span></div></div> : <div className="policy-grid">{policies.map(policy => <article className="policy-card" key={policy.id}>
      <div className="policy-card-header">
        <div className="policy-title-group">
          <Tags size={16} strokeWidth={1.7} aria-hidden="true" />
          <button className="policy-name" title={policy.name} onClick={() => onEdit(policy)}>{policy.name}</button>
        </div>
        <ActionMenu label={`${policy.name} 操作`} items={[
          { label: "编辑策略", action: () => onEdit(policy) },
          { label: "删除策略", destructive: true, action: () => { setDeleteError(null); setDeleting(policy); } },
        ]} />
      </div>
      <p className="policy-description" title={policy.description || policy.tags.map(tag => tag.description).join("；")}>
        {policy.description || policy.tags.map(tag => tag.description).join("；")}
      </p>
      <div className="policy-label-preview">
        {policy.tags.slice(0, 4).map(tag => <span key={tag.id} title={`${tag.description} · 阈值 ${tag.threshold ?? policy.threshold}`}>{tag.label}</span>)}
        {policy.tags.length > 4 && <span title={policy.tags.slice(4).map(tag => tag.label).join("、")}>+{policy.tags.length - 4}</span>}
      </div>
      <div className="policy-facts">
        <span>{policy.mode === "multi_label" ? "多标签" : "单标签"}</span><i />
        <span>{formatNumber(policy.tags.length)} 个标签</span><i />
        <span><strong>{formatNumber(policy.result_count || 0)}</strong> 个已分类仓库</span>
      </div>
      <div className="policy-card-footer">
        <span>{formatDate(policy.updated_at)} 更新</span>
        <div className="policy-footer-actions">
          <Button variant="ghost" size="sm" onClick={() => onResults(policy)}><ListFilter size={13} />查看结果</Button>
          <Button variant="ghost" size="sm" onClick={() => onRun(policy)}>开始分类<ArrowRight size={14} /></Button>
        </div>
      </div>
    </article>)}</div>}
    <Dialog open={Boolean(deleting)} onOpenChange={open => { if (!open && !busy) setDeleting(null); }}><DialogContent className="small-dialog"><div className="dialog-heading"><DialogTitle>删除策略？</DialogTitle><DialogDescription>“{deleting?.name}”将从策略列表移除，历史任务记录会保留。</DialogDescription></div><div className="dialog-body"><InlineError message={deleteError} /></div><div className="dialog-footer"><span /><div><Button variant="outline" disabled={busy} onClick={() => setDeleting(null)}>取消</Button><Button variant="destructive" disabled={busy} onClick={remove}>{busy && <Loader2 size={14} className="spin" />}删除策略</Button></div></div></DialogContent></Dialog>
  </>;
}
