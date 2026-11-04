import { useEffect, useState } from "react";
import { ArrowUpRight, Check, FolderGit2, Layers2, ListTodo, LogOut, RefreshCw, Tags, X } from "lucide-react";
import { api, type Policy } from "./lib/api";
import { emptyFilters } from "./lib/domain";
import { useResource } from "./hooks/use-resource";
import { Button } from "./components/ui/button";
import { PolicyEditor } from "./components/policy-editor";
import { RunDialog } from "./components/run-dialog";
import { RepositoryDetailDrawer } from "./components/repository-detail";
import { PolicyResultsDrawer } from "./components/policy-results";
import { RepositoriesPage, type RunContext } from "./pages/repositories";
import { PoliciesPage } from "./pages/policies";
import { JobsPage } from "./pages/jobs";
import { AuthGate } from "./components/auth-gate";
import type { AuthSession } from "./lib/auth";

type Page = "repositories" | "policies" | "jobs";
const navigation = [
  { id: "repositories" as Page, label: "Agent 目录", icon: FolderGit2 },
  { id: "policies" as Page, label: "分类策略", icon: Tags },
  { id: "jobs" as Page, label: "分类任务", icon: ListTodo },
];
function initialPage(): Page {
  const name = window.location.hash.replace(/^#\/?/, "");
  return name === "policies" || name === "jobs" ? name : "repositories";
}

export default function App() {
  return <AuthGate>{(session, logout, loggingOut) => <Workspace session={session} onLogout={logout} loggingOut={loggingOut} />}</AuthGate>;
}

function Workspace({ session, onLogout, loggingOut }: { session: AuthSession; onLogout: () => Promise<void>; loggingOut: boolean }) {
  const [page, setPage] = useState<Page>(initialPage);
  const [refresh, setRefresh] = useState(0);
  const [poll, setPoll] = useState(0);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingPolicy, setEditingPolicy] = useState<Policy | null>(null);
  const [resultsPolicy, setResultsPolicy] = useState<Policy | null>(null);
  const [runContext, setRunContext] = useState<RunContext | null>(null);
  const [repositoryId, setRepositoryId] = useState<number | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const stats = useResource(signal => api.stats(signal), [refresh, poll]);
  const health = useResource(signal => api.health(signal), [refresh, poll]);
  const policies = useResource(signal => api.policies(signal), [refresh]);
  const policyItems = policies.data?.items || [];
  const configuredModel = Boolean(health.data?.jev_configured || health.data?.glm_configured || health.data?.models?.some(model => model.configured));
  const providerStatus = health.error ? "服务未连接" : !health.data ? "服务连接中" : configuredModel ? "自动分类可用" : "自动分类未配置";
  function refreshAll() { setRefresh(value => value + 1); }
  function navigate(next: Page) { window.location.hash = `/${next}`; setPage(next); }
  function createPolicy() {
    setEditingPolicy(null); setEditorOpen(true);
  }
  function policySaved(policy: Policy) {
    refreshAll(); setToast(`已保存策略“${policy.name}”`);
  }
  useEffect(() => {
    const changed = () => setPage(initialPage());
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  useEffect(() => { const timer = window.setInterval(() => setPoll(value => value + 1), 15000); return () => clearInterval(timer); }, []);
  useEffect(() => { if (toast) { const timer = window.setTimeout(() => setToast(null), 4500); return () => clearTimeout(timer); } }, [toast]);

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#/repositories" aria-label="Agendex 首页"><span className="brand-mark"><Layers2 size={21} strokeWidth={1.9} /></span><span>Agendex<span className="brand-dot">.</span></span></a>
      <div className="workspace-label">目录与分类</div>
      <nav aria-label="主导航">{navigation.map(item => <button key={item.id} title={item.label} className={`nav-item ${page === item.id ? "nav-active" : ""}`} onClick={() => navigate(item.id)} aria-current={page === item.id ? "page" : undefined}><item.icon size={17} strokeWidth={1.7} /><span>{item.label}</span>{item.id === "jobs" && Boolean(stats.data?.active_jobs) && <span className="nav-count">{stats.data!.active_jobs}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="provider-status" title={providerStatus}><i className={configuredModel ? "connected" : ""} /><span>{providerStatus}</span></div><div className={`workspace-footer ${session.enabled ? "is-authenticated" : ""}`}><div className="workspace-avatar">A</div><div><strong>Agendex</strong><span>{session.enabled ? session.username : "本地工作区"}</span></div>{session.enabled && <Button variant="ghost" size="icon" aria-label="退出登录" title="退出登录" disabled={loggingOut} onClick={() => { void onLogout().catch(() => setToast("退出失败，请重试。")); }}><LogOut size={14} /></Button>}</div></div>
    </aside>

    <main className="main-shell">
      <header className="main-header"><div className="breadcrumb"><span>Agendex</span><span>/</span><strong>{navigation.find(item => item.id === page)?.label}</strong></div><div className="header-actions"><a className="header-link" href="https://github.com" target="_blank" rel="noopener noreferrer" aria-label="打开 GitHub"><span>GitHub</span><ArrowUpRight size={14} /></a><Button variant="ghost" size="icon" aria-label="刷新数据" title="刷新数据" onClick={refreshAll}><RefreshCw size={15} className={stats.loading && stats.data ? "spin" : ""} /></Button></div></header>
      <div className="page-content">
        {page === "repositories" && <RepositoriesPage policies={policyItems} policiesLoading={policies.loading} policiesError={policies.error} onReloadPolicies={policies.reload} onCreatePolicy={() => createPolicy()} stats={stats.data} refresh={refresh} onRun={setRunContext} onDetails={setRepositoryId} />}
        {page === "policies" && <PoliciesPage policies={policyItems} loading={policies.loading} error={policies.error} onReload={policies.reload} onCreate={() => createPolicy()} onEdit={policy => { setEditingPolicy(policy); setEditorOpen(true); }} onResults={setResultsPolicy} onRun={policy => setRunContext({ selectedIds: [], filters: { ...emptyFilters }, total: stats.data?.total_repositories || 0, policyId: policy.id })} onDeleted={() => { refreshAll(); setToast("策略已删除，历史记录已保留"); }} />}
        {page === "jobs" && <JobsPage refresh={refresh} onChanged={refreshAll} onRepository={setRepositoryId} />}
      </div>
    </main>

    <PolicyEditor open={editorOpen} policy={editingPolicy} health={health.data} onOpenChange={setEditorOpen} onSaved={policySaved} />
    <RunDialog context={runContext} policies={policyItems} health={health.data} onClose={() => setRunContext(null)} onOpenPolicies={() => { setRunContext(null); navigate("policies"); }} onCreated={() => { refreshAll(); navigate("jobs"); setToast("已打开分类任务"); }} />
    <PolicyResultsDrawer policy={resultsPolicy} refresh={refresh + poll} onClose={() => setResultsPolicy(null)} onPolicyChanged={policy => { setResultsPolicy(policy); refreshAll(); setToast("策略新版本已保存"); }} onRepository={setRepositoryId} />
    <RepositoryDetailDrawer id={repositoryId} onClose={() => setRepositoryId(null)} refresh={refresh + poll} onChanged={refreshAll} />
    {toast && <div className="toast" role="status"><span className="toast-icon"><Check size={13} /></span><span>{toast}</span><button onClick={() => setToast(null)} aria-label="关闭通知"><X size={14} /></button></div>}
  </div>;
}
