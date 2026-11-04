import { useEffect, useRef, useState } from "react";
import { ArrowRight, Check, Loader2 } from "lucide-react";
import { api, type ClassifierModel, type Health, type Job, type Policy } from "@/lib/api";
import { classifierOptions, defaultClassifierModel } from "@/lib/classifiers";
import { createJobScope } from "@/lib/domain";
import { formatNumber } from "@/lib/utils";
import type { RunContext } from "@/pages/repositories";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";
import { Select } from "./ui/select";
import { InlineError } from "./shared";
import { EvidenceSourceSelect } from "./evidence-source-select";

export function RunDialog({ context, policies, health, onClose, onCreated, onOpenPolicies }: {
  context: RunContext | null; policies: Policy[]; health?: Health; onClose: () => void;
  onCreated: (job: Job) => void; onOpenPolicies: () => void;
}) {
  const [policyId, setPolicyId] = useState("");
  const [scopeMode, setScopeMode] = useState<"selected" | "filtered">("filtered");
  const [limit, setLimit] = useState(100);
  const [model, setModel] = useState<ClassifierModel>(defaultClassifierModel);
  const [evidenceKinds, setEvidenceKinds] = useState<string[]>(["metadata", "readme"]);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (context) {
      setPolicyId(context.policyId || policies[0]?.id || "");
      setScopeMode(context.scopeMode || (context.selectedIds.length ? "selected" : "filtered"));
      setLimit(context.limit ?? Math.min(100, context.selectedIds.length || context.total || 100));
      setModel(context.model || defaultClassifierModel);
      setEvidenceKinds(context.evidenceKinds || ["metadata", "readme"]);
      setError(null);
    }
  }, [context]);
  const selectedPolicy = policies.find(policy => policy.id === policyId);
  const models = classifierOptions(health);
  const selectedModel = models.find(option => option.id === model);
  const configured = Boolean(selectedModel?.configured);
  const available = scopeMode === "selected" ? context?.selectedIds.length || 0 : context?.total || 0;
  const effectiveLimit = Math.min(available, Number.isInteger(limit) && limit > 0 ? limit : 0);
  async function start(event: React.FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    if (!context || !selectedPolicy) { setError("请选择分类策略"); return; }
    if (!effectiveLimit) { setError("请选择有效的分类数量"); return; }
    if (!configured) { setError(`请先在服务端配置 ${selectedModel?.label || model}`); return; }
    submitting.current = true; setBusy(true); setError(null);
    try {
      const scope = createJobScope(scopeMode, context.selectedIds, context.filters, effectiveLimit);
      const job = await api.createJob(policyId, scope, true, model, evidenceKinds);
      onCreated(job); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "任务创建失败"); }
    finally { submitting.current = false; setBusy(false); }
  }
  return <Dialog open={Boolean(context)} onOpenChange={open => { if (!open && !busy) onClose(); }}><DialogContent className="run-dialog">
    <div className="dialog-heading"><DialogTitle>新建分类任务</DialogTitle><DialogDescription>选择已定义的策略，设置本次分类范围。</DialogDescription></div>
    <form onSubmit={start}>
      <div className="dialog-body run-form">
        <div className="form-field"><label>分类策略</label><Select value={policyId} label="选择分类策略" disabled={busy || !policies.length} onValueChange={setPolicyId} options={[
          { value: "", label: "选择分类策略" }, ...policies.map(policy => ({ value: policy.id, label: policy.name })),
        ]} />{!policies.length && <div className="run-policy-empty"><span>请先定义并保存分类策略。</span><Button type="button" variant="ghost" size="sm" onClick={onOpenPolicies}>前往分类策略<ArrowRight size={13} /></Button></div>}</div>
        <div className="form-row run-model-source"><div className="form-field"><label>分类模型</label><Select label="分类模型" value={model} disabled={busy} onValueChange={value => { setModel(value as ClassifierModel); setError(null); }} options={models.map(option => ({ value: option.id, label: option.label }))} /></div><div className="form-field"><label>数据来源</label><EvidenceSourceSelect kinds={evidenceKinds} disabled={busy} onChange={setEvidenceKinds} /></div></div>
        <div className="form-field"><label>分类范围</label><div className="scope-options">
          {context && context.selectedIds.length > 0 && <button type="button" className={`scope-option ${scopeMode === "selected" ? "scope-selected" : ""}`} onClick={() => setScopeMode("selected")}><span>选中的仓库<small>{formatNumber(context.selectedIds.length)} 个</small></span>{scopeMode === "selected" && <Check size={16} />}</button>}
          <button type="button" className={`scope-option ${scopeMode === "filtered" ? "scope-selected" : ""}`} onClick={() => setScopeMode("filtered")}><span>当前筛选结果<small>{formatNumber(context?.total)} 个</small></span>{scopeMode === "filtered" && <Check size={16} />}</button>
        </div></div>
        <div className="form-field"><label htmlFor="run-limit">本次最多分类</label><div className="number-input-row"><input id="run-limit" className="input" type="number" min={1} max={Math.max(1, available)} step={1} required value={Number.isNaN(limit) ? "" : limit} onChange={event => setLimit(event.target.value === "" ? NaN : Number(event.target.value))} /><span>个仓库</span><Button variant="ghost" size="sm" type="button" onClick={() => setLimit(available)}>全部</Button></div></div>
        <div className="run-summary"><span>将提交 <strong>{formatNumber(effectiveLimit)}</strong> 个仓库</span><span>{selectedPolicy?.tags.length || 0} 个标签 · {selectedModel?.label || model}</span></div>
        {!configured && <div className="connection-warning">{selectedModel?.label || model} 尚未配置，请检查服务端配置。</div>}
        <InlineError message={error} />
      </div>
      <div className="dialog-footer"><span className="subtle-text">已有分类结果自动复用</span><div><Button type="button" variant="outline" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" disabled={busy || !selectedPolicy || !effectiveLimit || !configured}>{busy && <Loader2 className="spin" size={15} />}开始分类<ArrowRight size={14} /></Button></div></div>
    </form>
  </DialogContent></Dialog>;
}
