import { useRef, useState } from "react";
import { Loader2, RotateCcw } from "lucide-react";
import { api, type AnnotationPolicy } from "@/lib/api";
import { useResource } from "@/hooks/use-resource";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import { Select } from "./ui/select";
import { ErrorState, InlineError, Loading } from "./shared";

type Overrides = Record<string, boolean>;

function savedOverrides(policy: AnnotationPolicy): Overrides {
  return Object.fromEntries(policy.tags.filter(tag => tag.manual_applied !== null)
    .map(tag => [tag.tag_id, tag.manual_applied as boolean]));
}

/** Human decisions are stored separately; saving never invokes a classifier. */
export function ManualTagEditor({ repositoryId, onCancel, onSaved }: {
  repositoryId: number; onCancel: () => void; onSaved: () => void;
}) {
  const resource = useResource(signal => api.repositoryAnnotations(repositoryId, signal), [repositoryId]);
  const [policyId, setPolicyId] = useState("");
  const [drafts, setDrafts] = useState<Record<string, Overrides>>({});
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const policies = resource.data?.items || [];
  const policy = policies.find(item => item.policy_id === policyId) || policies[0];
  const overrides = policy ? drafts[policy.policy_id] || savedOverrides(policy) : {};
  const manualWinner = policy?.mode === "single_label" ? policy.tags.find(tag => overrides[tag.tag_id] === true)?.tag_id : undefined;

  function update(next: Overrides) {
    if (policy) setDrafts(previous => ({ ...previous, [policy.policy_id]: next }));
    setError(null);
  }
  function choose(tagId: string, applied: boolean) {
    const next = { ...overrides, [tagId]: applied };
    if (policy?.mode === "single_label" && applied) {
      for (const tag of policy.tags) {
        if (tag.tag_id !== tagId && next[tag.tag_id] === true) delete next[tag.tag_id];
      }
    }
    update(next);
  }
  async function save() {
    if (!policy || submitting.current) return;
    submitting.current = true;
    setBusy(true); setError(null);
    try {
      await api.saveRepositoryAnnotations(repositoryId, policy.policy_id, policy.policy_revision,
        Object.entries(overrides).map(([tag_id, applied]) => ({ tag_id, applied })));
      onSaved();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "标签保存失败"); }
    finally { submitting.current = false; setBusy(false); }
  }

  return <div className="manual-tag-editor" aria-label="编辑仓库标签">
    {resource.error ? <ErrorState message={resource.error} onRetry={resource.reload} /> : !resource.data ? <Loading label="正在读取标签" /> : !policy ? <p className="detail-empty">请先创建分类策略。</p> : <>
      <Select label="标签所属策略" value={policy.policy_id} disabled={busy} onValueChange={value => { setPolicyId(value); setError(null); }}
        options={policies.map(item => ({ value: item.policy_id, label: item.policy_name }))} />
      <div className="manual-tag-options">{policy.tags.map(tag => {
        const explicit = overrides[tag.tag_id];
        const suppressed = Boolean(manualWinner && manualWinner !== tag.tag_id);
        const applied = suppressed ? false : explicit ?? tag.model_applied;
        const score = typeof explicit === "boolean" || suppressed ? (applied ? 1 : 0) : tag.model_score;
        return <div className="manual-tag-option" key={tag.tag_id}>
          <label><Checkbox checked={applied} disabled={busy} aria-label={tag.label} onCheckedChange={checked => choose(tag.tag_id, checked === true)} />
            <span title={tag.description}>{tag.label}</span></label>
          <span className="manual-tag-score">{typeof explicit === "boolean" || suppressed ? "人工" : "模型"} · {score == null ? "未评分" : score.toFixed(2)}</span>
          {typeof explicit === "boolean" && <Button type="button" variant="ghost" size="icon" disabled={busy} aria-label={`恢复 ${tag.label} 自动结果`} onClick={() => {
            const next = { ...overrides }; delete next[tag.tag_id]; update(next);
          }}><RotateCcw size={12} /></Button>}
        </div>;
      })}</div>
      <p className="manual-tag-note">人工标签得分为 1.00，自动分类不会覆盖。</p>
      <InlineError message={error} />
      {error && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => { setDrafts({}); resource.reload(); }}>重新加载</Button>}
    </>}
    <div className="manual-tag-actions"><Button type="button" variant="ghost" size="sm" disabled={busy || !policy} onClick={() => update({})}>恢复自动结果</Button><div>
      <Button type="button" variant="outline" size="sm" disabled={busy} onClick={onCancel}>取消</Button>
      <Button type="button" size="sm" disabled={busy || !policy || Boolean(resource.error)} onClick={save}>{busy && <Loader2 size={13} className="spin" />}保存标签</Button>
    </div></div>
  </div>;
}
