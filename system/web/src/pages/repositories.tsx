import { useEffect, useState } from "react";
import { ArrowDownWideNarrow, ArrowRight, ArrowUpRight, ChevronLeft, ChevronRight, Download, FilterX, FolderGit2, Loader2, Plus, Search, Star, Tags, X } from "lucide-react";
import { api, type ClassifierModel, type Policy, type RepositoryFilters, type Stats } from "@/lib/api";
import { emptyFilters, selectedTagIds, toggleTagFilter } from "@/lib/domain";
import { formatDate, formatNumber, compactNumber } from "@/lib/utils";
import { useResource } from "@/hooks/use-resource";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Select } from "@/components/ui/select";
import { EmptyState, ErrorState, InlineError, Loading } from "@/components/shared";
import { DirectoryPolicyShortcuts } from "@/components/directory-policy-shortcuts";
import { TagFilterSelect } from "@/components/tag-filter-select";

export interface RunContext {
  selectedIds: number[]; filters: RepositoryFilters; total: number; policyId?: string;
  scopeMode?: "selected" | "filtered"; limit?: number;
  model?: ClassifierModel; evidenceKinds?: string[];
}

export function RepositoriesPage({ policies, policiesLoading, policiesError, onReloadPolicies, onCreatePolicy, stats, refresh, onRun, onDetails }: {
  policies: Policy[]; stats?: Stats; refresh: number; onRun: (context: RunContext) => void; onDetails: (id: number) => void;
  policiesLoading: boolean; policiesError: string | null; onReloadPolicies: () => void; onCreatePolicy: () => void;
}) {
  const [filters, setFilters] = useState<RepositoryFilters>({ ...emptyFilters });
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setFilters(previous => previous.q === search.trim() ? previous : { ...previous, q: search.trim() });
      setPage(1); setSelected(new Set());
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);
  const resource = useResource(signal => api.repositories(filters, page, pageSize, signal), [filters, page, pageSize, refresh]);
  const data = resource.data;
  const selectedPolicy = policies.find(policy => policy.id === filters.policy_id);
  const tagIds = selectedTagIds(filters);
  const filtered = Boolean(filters.q || filters.language || filters.min_stars || filters.policy_id || tagIds.length);

  async function downloadAll() {
    setDownloading(true); setDownloadError(null);
    try { await api.downloadRepositories(); }
    catch (reason) { setDownloadError(reason instanceof Error ? reason.message : "列表下载失败"); }
    finally { setDownloading(false); }
  }

  function updateFilters(patch: Partial<RepositoryFilters>) {
    setFilters(previous => ({ ...previous, ...patch })); setPage(1); setSelected(new Set());
  }
  function toggleTag(policyId: string, tagId: string) {
    setFilters(previous => toggleTagFilter(previous, policyId, tagId)); setPage(1); setSelected(new Set());
  }
  function clearFilters() { setSearch(""); setFilters({ ...emptyFilters }); setPage(1); setSelected(new Set()); }
  function goToPage(next: number) { setPage(next); setSelected(new Set()); }
  function toggleOne(id: number, checked: boolean) {
    setSelected(previous => { const copy = new Set(previous); if (checked) copy.add(id); else copy.delete(id); return copy; });
  }
  const items = data?.items || [];
  const allSelected = items.length > 0 && items.every(item => selected.has(item.id));
  const someSelected = items.some(item => selected.has(item.id));
  const totalPages = Math.max(1, Math.ceil((data?.total || 0) / pageSize));
  return <>
    <div className="page-heading">
      <div><h1>Agent 目录</h1></div>
      <div className="page-heading-actions">
        <Button variant="outline" onClick={downloadAll} disabled={downloading || !stats?.total_repositories}>
          {downloading ? <Loader2 size={15} className="spin" /> : <Download size={15} />}下载全部列表
        </Button>
        <Button onClick={() => onRun({ selectedIds: [...selected], filters, total: data?.total || 0 })} disabled={!data?.total}>
          新建分类任务<ArrowRight size={15} />
        </Button>
      </div>
    </div>
    <InlineError message={downloadError} />
    <section className="directory-policies" aria-label="分类策略">
      {policiesError ? <div className="directory-policies-empty"><Tags size={20} /><div><h2>暂时无法读取分类策略</h2><p>{policiesError}</p></div><Button variant="outline" size="sm" onClick={onReloadPolicies}>重新加载</Button></div>
        : policiesLoading && !policies.length ? <div className="directory-policies-empty" aria-busy="true"><Loader2 size={18} className="spin" /><p>正在读取分类策略…</p></div>
        : !policies.length ? <div className="directory-policies-empty"><Tags size={22} /><div><h2>尚未创建分类策略</h2><p>创建分类策略，为 Agent 建立标签体系。</p></div><Button variant="outline" size="sm" onClick={onCreatePolicy}><Plus size={14} />新建策略</Button></div>
        : <DirectoryPolicyShortcuts policies={policies} selectedPolicyId={filters.policy_id} selectedTagIds={tagIds} onSelectPolicy={policy_id => updateFilters({ policy_id, tag_id: "", tag_ids: [] })} onSelectTag={tag_id => toggleTag(filters.policy_id, tag_id)} onCreatePolicy={onCreatePolicy} />}
    </section>
    <section className="repository-panel" aria-label="仓库目录">
      <div className="table-toolbar">
        <div className="search-field"><Search size={16} /><input aria-label="搜索仓库" placeholder="搜索仓库、描述或 Topics…" value={search} onChange={event => setSearch(event.target.value)} />{search && <button aria-label="清空搜索" onClick={() => setSearch("")}><X size={14} /></button>}</div>
        <div className="toolbar-filters">
          <Select value={filters.language} label="编程语言" onValueChange={language => updateFilters({ language })} options={[
            { value: "", label: "全部语言" }, ...(stats?.languages || []).map(language => ({ value: language.name, label: language.name })),
          ]} />
          <Select value={String(filters.min_stars)} label="最低 Star 数" onValueChange={value => updateFilters({ min_stars: Number(value) })} options={[
            { value: "0", label: "全部 Star" }, { value: "1000", label: "1,000+ Star" }, { value: "5000", label: "5,000+ Star" }, { value: "10000", label: "10,000+ Star" }, { value: "50000", label: "50,000+ Star" },
          ]} />
          <Select value={filters.policy_id} label="分类策略筛选" onValueChange={policy_id => updateFilters({ policy_id, tag_id: "", tag_ids: [] })} options={[
            { value: "", label: "全部策略" }, ...policies.map(policy => ({ value: policy.id, label: policy.name })),
          ]} />
          {selectedPolicy && <TagFilterSelect tags={selectedPolicy.tags} selectedIds={tagIds} onToggle={tag_id => toggleTag(selectedPolicy.id, tag_id)} onClear={() => toggleTag(selectedPolicy.id, "")} />}
          {filtered && <Button size="icon" variant="ghost" aria-label="清除全部筛选" onClick={clearFilters}><FilterX size={16} /></Button>}
        </div>
      </div>
      <div className="table-meta">
        <div>{selected.size ? <span className="selected-count">已选择 {selected.size} 个仓库<Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>取消选择</Button></span> : <span>共 <strong>{formatNumber(data?.total)}</strong> 个仓库</span>}</div>
        <div className="sort-control"><ArrowDownWideNarrow size={14} /><Select value={filters.sort} label="排序方式" onValueChange={sort => updateFilters({ sort: sort as RepositoryFilters["sort"] })} options={[
          { value: "stars", label: "Star 最多" }, { value: "updated", label: "最近更新" }, { value: "name", label: "名称排序" },
        ]} /></div>
      </div>
      {resource.error ? <ErrorState message={resource.error} onRetry={resource.reload} /> : !data && resource.loading ? <Loading label="正在读取仓库" /> : !items.length ? selectedPolicy && !selectedPolicy.result_count
        ? <EmptyState title="该策略尚无分类结果" description="创建分类任务后，即可按这套标签浏览仓库。"><Button variant="outline" size="sm" disabled={!stats?.total_repositories} onClick={() => onRun({ selectedIds: [], filters: { ...emptyFilters }, total: stats?.total_repositories || 0, policyId: selectedPolicy.id })}>新建分类任务<ArrowRight size={14} /></Button></EmptyState>
        : <EmptyState title="没有匹配的仓库" description="试试其他关键词或筛选条件。"><Button variant="outline" size="sm" onClick={clearFilters}>清除筛选</Button></EmptyState> : <>
        <div className={`table-scroll ${resource.loading ? "is-refreshing" : ""}`} aria-busy={resource.loading} tabIndex={0} role="region" aria-label="仓库列表">
          <table className="repository-table">
            <thead><tr><th className="checkbox-cell"><Checkbox aria-label="选择本页全部仓库" checked={allSelected ? true : someSelected ? "indeterminate" : false} onCheckedChange={checked => setSelected(checked ? new Set(items.map(item => item.id)) : new Set())} /></th><th className="repository-name-column">仓库</th><th className="tags-cell">标签</th><th className="number-cell">Star</th><th className="language-cell">语言</th><th className="date-cell">更新</th></tr></thead>
            <tbody>{items.map(repository => <tr key={repository.id} data-selected={selected.has(repository.id)}>
              <td className="checkbox-cell"><Checkbox aria-label={`选择 ${repository.full_name}`} checked={selected.has(repository.id)} onCheckedChange={checked => toggleOne(repository.id, Boolean(checked))} /></td>
              <td className="repository-main"><div className="repository-title"><button className="repo-name" title={repository.full_name} onClick={() => onDetails(repository.id)}><FolderGit2 size={15} /><span>{repository.full_name}</span></button><a className="repository-external" href={repository.url} target="_blank" rel="noopener noreferrer" aria-label={`在 GitHub 打开 ${repository.full_name}`} title="在 GitHub 打开"><ArrowUpRight size={13} /></a></div><p title={repository.description || "暂无描述"}>{repository.description || "暂无描述"}</p></td>
              <td className="tags-cell">{repository.tags?.length ? <div className="tag-list">{repository.tags.slice(0, 2).map(tag => <button className="tag" title={`${tag.label} · ${tag.source === "manual" ? "人工标注 1.00" : `模型分数 ${tag.score.toFixed(2)}`}`} key={`${tag.policy_id}-${tag.tag_id}`} onClick={() => toggleTag(tag.policy_id, tag.tag_id)}>{tag.label}{tag.source === "manual" && <small className="manual-tag-mark">人工</small>}</button>)}{repository.tags.length > 2 && <span className="more-tags">+{repository.tags.length - 2}</span>}</div> : <span className="unclassified">—</span>}</td>
              <td className="number-cell"><span className="star-count" title={`${formatNumber(repository.stars)} Star`}><Star size={12} />{compactNumber(repository.stars)}</span></td>
              <td className="language-cell"><span className="language"><i style={{ backgroundColor: languageColor(repository.language) }} />{repository.language || "—"}</span></td>
              <td className="date-cell">{formatDate(repository.pushed_at || repository.updated_at)}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <div className="pagination"><div className="pagination-size"><span>每页</span><Select value={String(pageSize)} label="每页显示数量" onValueChange={value => { setPageSize(Number(value)); goToPage(1); }} options={[25, 50, 100].map(value => ({ value: String(value), label: String(value) }))} /><span>条</span></div><div className="pagination-controls"><span>第 {page} / {formatNumber(totalPages)} 页</span><Button variant="outline" size="icon" aria-label="上一页" disabled={page === 1 || resource.loading} onClick={() => goToPage(page - 1)}><ChevronLeft size={16} /></Button><Button variant="outline" size="icon" aria-label="下一页" disabled={page >= totalPages || resource.loading} onClick={() => goToPage(page + 1)}><ChevronRight size={16} /></Button></div></div>
      </>}
    </section>
    <div className="page-footnote">GitHub 仓库</div>
  </>;
}

function languageColor(language: string | null) {
  const colors: Record<string, string> = { Python: "#3572a5", TypeScript: "#3178c6", JavaScript: "#c5ac35", Rust: "#b77353", Go: "#2795a9", Java: "#ad6635", Shell: "#6b8649", "C++": "#b65886", HTML: "#c75e42" };
  return colors[language || ""] || "#737373";
}
