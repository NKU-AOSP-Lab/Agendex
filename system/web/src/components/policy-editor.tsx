import { useEffect, useRef, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import { api, type Health, type Policy, type PolicyDraft } from "@/lib/api";
import { newPolicyDraft, policyValidation, tagThreshold } from "@/lib/domain";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";
import { Select } from "./ui/select";
import { InlineError } from "./shared";
import { PolicyAssistant } from "./policy-assistant";

export function PolicyEditor({ open, policy, health, onOpenChange, onSaved }: {
  open: boolean; policy: Policy | null; onOpenChange: (open: boolean) => void; onSaved: (policy: Policy) => void;
  health?: Health;
}) {
  const [draft, setDraft] = useState<PolicyDraft>(newPolicyDraft);
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [assistantSession, setAssistantSession] = useState(0);
  const submitting = useRef(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setDraft(policy ? { name: policy.name, description: policy.description, mode: policy.mode, threshold: policy.threshold,
        tags: policy.tags.map(tag => ({ ...tag, threshold: tagThreshold(tag, policy.threshold) })), evidence_kinds: [...policy.evidence_kinds] } : newPolicyDraft());
      setAssistantSession(value => value + 1); setGenerating(false);
      setError(null);
    }
  }, [open, policy?.id]);

  function clearDraft() {
    setDraft(newPolicyDraft()); setError(null);
    setAssistantSession(value => value + 1); setGenerating(false);
  }

  function updateTag(index: number, patch: { label?: string; description?: string; threshold?: number }) {
    setDraft(previous => ({ ...previous, tags: previous.tags.map((tag, position) => position === index ? { ...tag, ...patch } : tag) }));
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (submitting.current || generating) return;
    const validation = policyValidation(draft);
    if (validation) { setError(validation); return; }
    submitting.current = true; setBusy(true); setError(null);
    try {
      const saved = await api.savePolicy({ ...draft, name: draft.name.trim(), description: draft.description.trim(),
        tags: draft.tags.map(tag => ({ ...tag, label: tag.label.trim(), description: tag.description.trim() })) }, policy?.id);
      onSaved(saved); onOpenChange(false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); }
    finally { submitting.current = false; setBusy(false); }
  }
  return <Dialog open={open} onOpenChange={next => { if (!busy) onOpenChange(next); }}><DialogContent className="policy-dialog">
    <div className="dialog-heading"><DialogTitle>{policy ? "编辑分类策略" : "新建分类策略"}</DialogTitle><DialogDescription>定义标签与规则，或用自然语言生成。</DialogDescription></div>
    <form onSubmit={save}>
      <div className="dialog-body policy-form">
        <PolicyAssistant key={assistantSession} draft={draft} configured={Boolean(health?.glm_configured || health?.models?.find(model => model.id === "glm-5.3")?.configured)} disabled={busy} onBusyChange={setGenerating} onGenerated={generated => { setDraft(generated); setError(null); }} />
        <fieldset className="policy-fields" disabled={busy || generating}>
        <div className="form-row policy-identity-row">
          <div className="form-field"><label htmlFor="policy-name">策略名称<span>*</span></label><input id="policy-name" className="input" placeholder="例如：实现形式" value={draft.name} maxLength={120} required onChange={event => setDraft({ ...draft, name: event.target.value })} /></div>
          <div className="form-field"><label>分类方式</label><Select value={draft.mode} label="分类方式" onValueChange={mode => setDraft({ ...draft, mode: mode as PolicyDraft["mode"] })} options={[{ value: "multi_label", label: "多标签" }, { value: "single_label", label: "单标签" }]} /></div>
        </div>
        <div className="form-field"><label htmlFor="policy-description">公共说明 <small>可选</small></label><textarea id="policy-description" className="textarea" rows={2} placeholder="所有标签共同遵循的规则" value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} /></div>
        <div className="form-section-title"><h3>标签</h3><span>{draft.tags.length} 个</span></div>
        <div className="tag-editors">{draft.tags.map((tag, index) => <div className="tag-editor" key={tag.id}>
          <div className="tag-editor-top"><span className="tag-index">{String(index + 1).padStart(2, "0")}</span><input aria-label={`标签 ${index + 1} 名称`} className="input" placeholder="标签名称" value={tag.label} maxLength={80} required onChange={event => updateTag(index, { label: event.target.value })} /><label className="tag-threshold-control"><span>阈值</span><input aria-label={`标签 ${index + 1} 阈值`} className="input" type="number" min={0} max={1} step={0.01} required value={Number.isFinite(tag.threshold) ? tag.threshold : ""} onChange={event => updateTag(index, { threshold: event.target.value === "" ? NaN : Number(event.target.value) })} /></label><Button type="button" variant="ghost" size="icon" aria-label={`删除标签 ${index + 1}`} disabled={draft.tags.length === 1} onClick={() => setDraft({ ...draft, tags: draft.tags.filter((_, position) => position !== index) })}><Trash2 size={15} /></Button></div>
          <textarea aria-label={`标签 ${index + 1} 判断规则`} className="textarea" rows={3} placeholder="什么情况下应打这个标签？请写清纳入与排除条件。" value={tag.description} required onChange={event => updateTag(index, { description: event.target.value })} />
        </div>)}</div>
        <Button type="button" variant="outline" size="sm" className="add-tag-button" onClick={() => setDraft({ ...draft, tags: [...draft.tags, { id: crypto.randomUUID(), label: "", description: "", threshold: 0.8 }] })}><Plus size={14} />添加标签</Button>
        </fieldset>
        <InlineError message={error} />
      </div>
      <div className="dialog-footer"><span className="subtle-text">{generating ? "GLM 5.3 正在生成" : "保存后可用于分类任务"}</span><div><Button type="button" variant="ghost" disabled={busy} onClick={clearDraft}>清空</Button><Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>取消</Button><Button type="submit" disabled={busy || generating}>{busy && <Loader2 size={15} className="spin" />}保存策略</Button></div></div>
    </form>
  </DialogContent></Dialog>;
}
