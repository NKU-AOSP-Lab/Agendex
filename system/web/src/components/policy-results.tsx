import { useEffect, useState } from "react";
import { ArrowUpRight, Check, ChevronLeft, ChevronRight, Download, Loader2, RefreshCw, Search } from "lucide-react";
import { api, type Policy, type PolicyResult } from "@/lib/api";
import { previewCount, previewResult, thresholdMap, thresholdsValid } from "@/lib/domain";
import { compactNumber, formatDate, formatNumber } from "@/lib/utils";
import { useResource } from "@/hooks/use-resource";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";
import { Select } from "./ui/select";
import { EmptyState, ErrorState, InlineError, Loading } from "./shared";

/** Threshold editing only reuses saved scores; this component never creates a job. */
export function PolicyResultsDrawer({ policy, refresh, onClose, onPolicyChanged, onRepository }: {
  policy: Policy | null; refresh: number; onClose: () => void;
  onPolicyChanged: (policy: Policy) => void; onRepository: (id: number) => void;
}) {
  const [revision, setRevision] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [reload, setReload] = useState(0);
  const [thresholds, setThresholds] = useState<Record<string, number>>({});
  const [draftKey, setDraftKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  useEffect(() => {
    setRevision(null); setPage(1); setSearch(""); setQuery(""); setError(null); setDownloadError(null); setThresholds({}); setDraftKey("");
  }, [policy?.id]);
  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(1); }, 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const versions = useResource(signal => policy ? api.policyVersions(policy.id, signal) : Promise.resolve(undefined), [policy?.id, reload, refresh]);
  const results = useResource(signal => policy ? api.policyResults(policy.id, revision ?? undefined, query, page, signal) : Promise.resolve(undefined), [policy?.id, revision, query, page, reload, refresh]);
  const versionItems = [...(versions.data?.items || (policy ? [policy] : []))].sort((left, right) => right.revision - left.revision);
  const currentRevision = Math.max(policy?.revision || 0, ...versionItems.map(item => item.revision));
  const expectedRevision = revision ?? currentRevision;
  const data = results.data?.policy.id === policy?.id && results.data?.policy.revision === expectedRevision ? results.data : undefined;
  const shownPolicy = data?.policy;
  const historical = Boolean(shownPolicy && shownPolicy.revision !== currentRevision);
  const savedThresholds = shownPolicy ? thresholdMap(shownPolicy) : {};
  const shownKey = shownPolicy ? `${shownPolicy.id}:${shownPolicy.revision}` : "";
  const activeThresholds = draftKey === shownKey ? thresholds : savedThresholds;
  const dirty = Boolean(shownPolicy && Object.keys(savedThresholds).some(id => activeThresholds[id] !== savedThresholds[id]));
  const valid = thresholdsValid(activeThresholds);

  useEffect(() => {
    if (shownPolicy) { setThresholds(thresholdMap(shownPolicy)); setDraftKey(`${shownPolicy.id}:${shownPolicy.revision}`); setError(null); }
    // Paging, searching or refreshing the same revision must not discard a local preview.
  }, [shownPolicy?.id, shownPolicy?.revision]);

  function updateThreshold(id: string, value: number) {
    setThresholds({ ...activeThresholds, [id]: value }); setDraftKey(shownKey); setError(null);
  }
  function reloadCurrent() { setRevision(null); setReload(value => value + 1); setError(null); }
  async function apply() {
    if (!policy || !shownPolicy || historical || !valid) return;
    setBusy(true); setError(null);
    try {
      const saved = await api.applyThresholds(policy.id, shownPolicy.revision, activeThresholds);
      setThresholds(thresholdMap(saved)); setRevision(null); setReload(value => value + 1);
      onPolicyChanged(saved);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "阈值保存失败"); }
    finally { setBusy(false); }
  }
  async function restore() {
    if (!policy || !shownPolicy) return;
    setBusy(true); setError(null);
    try {
      const saved = await api.restorePolicy(policy.id, shownPolicy.revision);
      setThresholds(thresholdMap(saved)); setRevision(null); setReload(value => value + 1);
      onPolicyChanged(saved);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "版本复用失败"); }
    finally { setBusy(false); }
  }
  async function downloadResults() {
    if (!shownPolicy || !valid || !data?.total) return;
    setDownloading(true); setDownloadError(null);
    try { await api.downloadPolicyResults(shownPolicy.id, shownPolicy.revision, query, dirty ? activeThresholds : undefined); }
    catch (reason) { setDownloadError(reason instanceof Error ? reason.message : "分类结果下载失败"); }
    finally { setDownloading(false); }
  }
  const tagStats = new Map((data?.tag_stats || []).map(stats => [stats.tag_id, stats]));
  const totalPages = Math.max(1, Math.ceil((data?.total || 0) / 20));

  return <Dialog open={Boolean(policy)} onOpenChange={open => { if (!open && !busy) onClose(); }}><DialogContent drawer className="policy-results-drawer">
    <div className="drawer-heading results-heading"><div><DialogTitle>{policy?.name || "策略结果"}</DialogTitle><DialogDescription className="sr-only">查看保存的评分，预览标签阈值，并复用历史版本</DialogDescription></div><div className="results-version-controls"><Select value={String(expectedRevision || "")} label="策略版本" disabled={busy} onValueChange={value => { setRevision(Number(value)); setPage(1); setError(null); }} options={versionItems.map(item => ({ value: String(item.revision), label: `v${item.revision}${item.revision === currentRevision ? " · 当前" : ` · ${formatDate(item.updated_at || item.created_at)}`}` }))} /><Button variant="ghost" size="icon" aria-label="刷新策略结果" disabled={busy} onClick={() => setReload(value => value + 1)}><RefreshCw size={14} className={results.loading && data ? "spin" : ""} /></Button></div></div>
    {results.error ? <ErrorState message={results.error} onRetry={results.reload} /> : !data ? <Loading label="正在读取保存的结果" /> : <>
      <div className="results-body">
        <div className="results-summary"><span>已分类 <strong>{formatNumber(shownPolicy?.result_count ?? data.total)}</strong> 个仓库</span><span>{historical ? "历史版本" : dirty ? "阈值预览" : "当前版本"}</span></div>
        <section className="threshold-panel" aria-label="标签阈值">
          {shownPolicy!.tags.map(tag => {
            const stats = tagStats.get(tag.id) || { tag_id: tag.id, label: tag.label, threshold: savedThresholds[tag.id], scored_count: 0, applied_count: 0, distribution: [] };
            const threshold = activeThresholds[tag.id] ?? savedThresholds[tag.id];
            const count = previewCount(stats, threshold);
            return <div className="threshold-row" key={tag.id}>
              <span className="threshold-label" title={tag.description}>{tag.label}</span>
              <input className="threshold-range" type="range" min={0} max={1} step={0.01} aria-label={`${tag.label} 阈值滑块`} value={Number.isFinite(threshold) ? threshold : savedThresholds[tag.id]} disabled={historical || busy} onChange={event => updateThreshold(tag.id, Number(event.target.value))} />
              <input className="input threshold-number" type="number" min={0} max={1} step={0.01} aria-label={`${tag.label} 阈值`} value={Number.isFinite(threshold) ? threshold : ""} disabled={historical || busy} onChange={event => updateThreshold(tag.id, event.target.value === "" ? NaN : Number(event.target.value))} />
              <span className="threshold-count">{count == null ? "—" : formatNumber(count)} <small>个保留</small></span>
            </div>;
          })}
        </section>
        <div className="results-search-row"><div className="search-field"><Search size={15} /><input aria-label="搜索策略结果" value={search} placeholder="搜索已分类仓库…" onChange={event => setSearch(event.target.value)} /></div><div className="results-export-actions"><span>{formatNumber(data.total)} 个结果</span><Button variant="outline" size="sm" onClick={downloadResults} disabled={downloading || busy || results.loading || !valid || !data.total || search.trim() !== query}>{downloading ? <Loader2 size={14} className="spin" /> : <Download size={14} />}下载当前分类结果</Button></div></div>
        <InlineError message={downloadError} />
        {data.items.length ? <div className="results-table-scroll" aria-busy={results.loading}><table className="results-table"><thead><tr><th>仓库</th><th>标签与分数</th><th>Star</th></tr></thead><tbody>{data.items.map(row => <ResultRow key={row.repository_id} row={row} mode={shownPolicy!.mode} thresholds={activeThresholds} onRepository={onRepository} />)}</tbody></table></div> : <EmptyState title="暂无保存的评分" description={query ? "试试其他搜索词。" : "完成分类后，可以在这里调整阈值。"} />}
        {data.total > 0 && <div className="pagination results-pagination"><span>第 {page} / {formatNumber(totalPages)} 页</span><div className="pagination-controls"><Button size="icon" variant="outline" aria-label="策略结果上一页" disabled={page <= 1 || results.loading} onClick={() => setPage(value => value - 1)}><ChevronLeft size={15} /></Button><Button size="icon" variant="outline" aria-label="策略结果下一页" disabled={page >= totalPages || results.loading} onClick={() => setPage(value => value + 1)}><ChevronRight size={15} /></Button></div></div>}
        <InlineError message={error} />{error && <Button size="sm" variant="ghost" onClick={reloadCurrent}>重新加载当前版本</Button>}
      </div>
      <div className="dialog-footer results-footer"><span className="subtle-text">{historical ? "历史版本只读" : dirty ? "本地预览，尚未应用" : "评分与策略版本均已保存"}</span><div>{historical ? <Button disabled={busy} onClick={restore}>{busy && <Loader2 size={14} className="spin" />}复用此版本</Button> : <><Button variant="outline" disabled={!dirty || busy} onClick={() => { setThresholds(savedThresholds); setError(null); }}>重置</Button><Button disabled={!dirty || !valid || busy} onClick={apply}>{busy && <Loader2 size={14} className="spin" />}应用阈值</Button></>}</div></div>
    </>}
  </DialogContent></Dialog>;
}

function ResultRow({ row, mode, thresholds, onRepository }: { row: PolicyResult; mode: Policy["mode"]; thresholds: Record<string, number>; onRepository: (id: number) => void }) {
  const preview = previewResult(row, mode, thresholds);
  const applied = preview.filter(result => result.applied);
  const winner = row.results.find(result => result.tag_id === row.winner_tag_id);
  return <tr><td><button className="result-repository-name" onClick={() => onRepository(row.repository_id)}>{row.full_name}</button><span className="result-repository-meta">{row.language || "—"}<i />{formatDate(row.scored_at)}</span></td><td><div className="tag-list">{applied.length ? applied.map(tag => <span className="tag" key={tag.tag_id}>{tag.label}{tag.source === "manual" && <small className="manual-tag-mark">人工</small>}</span>) : <span className="result-no-tag">未打标签</span>}</div><details className="result-scores"><summary>查看分数</summary>{mode === "single_label" && <p>{row.results.some(result => result.manual_applied === true) ? "人工选择" : "模型选择"}：{winner?.label || "不打标签"}</p>}<div>{preview.map(result => <span key={result.tag_id}><span>{result.applied && <Check size={11} />}{result.label}{result.source === "manual" && <small className="manual-tag-mark">人工</small>}</span><span>{result.score ?? "—"}<small> / {Number.isFinite(result.threshold) ? result.threshold : "—"}</small></span></span>)}</div><a href={row.url} target="_blank" rel="noopener noreferrer">GitHub<ArrowUpRight size={11} /></a></details></td><td>{compactNumber(row.stars)}</td></tr>;
}
