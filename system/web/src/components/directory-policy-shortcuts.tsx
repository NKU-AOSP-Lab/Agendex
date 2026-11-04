import { useEffect, useState } from "react";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Check, Plus, Search, SlidersHorizontal, Tags } from "lucide-react";
import type { Policy } from "@/lib/api";
import { Button } from "./ui/button";
import { HorizontalScroll } from "./ui/horizontal-scroll";

const FAVORITES_KEY = "agendex.directory.favorite-policies";

function readFavorites(): string[] | null {
  try {
    const stored = window.localStorage.getItem(FAVORITES_KEY);
    if (stored === null) return null;
    const value: unknown = JSON.parse(stored);
    return Array.isArray(value) && value.every(id => typeof id === "string")
      ? [...new Set(value)] : null;
  } catch { return null; }
}

/** Favorites affect only this browser's shortcuts, never policies or their results. */
export function DirectoryPolicyShortcuts({ policies, selectedPolicyId, selectedTagIds, onSelectPolicy, onSelectTag, onCreatePolicy }: {
  policies: Policy[]; selectedPolicyId: string; selectedTagIds: string[];
  onSelectPolicy: (id: string) => void; onSelectTag: (id: string) => void; onCreatePolicy: () => void;
}) {
  const [favoriteIds, setFavoriteIds] = useState<string[] | null>(readFavorites);
  const [query, setQuery] = useState("");
  const initialIds = policies.map(policy => policy.id);
  const ids = favoriteIds ?? initialIds;
  const favorites = ids.flatMap(id => policies.find(policy => policy.id === id) || []);
  const selectedPolicy = policies.find(policy => policy.id === selectedPolicyId);
  const matching = policies.filter(policy => policy.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  useEffect(() => {
    if (!policies.length) return;
    if (favoriteIds === null) { setFavoriteIds(policies.map(policy => policy.id)); return; }
    const active = favoriteIds.filter(id => policies.some(policy => policy.id === id));
    if (active.length !== favoriteIds.length) setFavoriteIds(active);
  }, [policies, favoriteIds]);
  useEffect(() => {
    if (favoriteIds === null) return;
    try { window.localStorage.setItem(FAVORITES_KEY, JSON.stringify(favoriteIds)); }
    catch { /* Restricted browser storage still permits changing this session's shortcuts. */ }
  }, [favoriteIds]);

  function toggle(id: string, checked: boolean) {
    setFavoriteIds(previous => {
      const current = previous ?? initialIds;
      if (!checked) return current.filter(value => value !== id);
      return current.includes(id) ? current : [...current, id];
    });
  }

  return <>
    <div className="directory-policies-toolbar">
      <h2>分类策略</h2>
      <HorizontalScroll className="policy-shortcuts" label="常用分类策略">
        {favorites.length ? favorites.map(policy => <button type="button" className={`policy-shortcut ${selectedPolicyId === policy.id ? "is-active" : ""}`} aria-pressed={selectedPolicyId === policy.id} key={policy.id} title={policy.name} onClick={() => onSelectPolicy(selectedPolicyId === policy.id ? "" : policy.id)}><Tags size={13} /><span>{policy.name}</span></button>) : <span className="policy-shortcuts-empty">暂未设置常用策略</span>}
      </HorizontalScroll>
      <div className="directory-policies-actions">
        <Menu.Root onOpenChange={open => { if (!open) setQuery(""); }}>
          <Menu.Trigger asChild><Button variant="ghost" size="sm"><SlidersHorizontal size={13} />管理常用</Button></Menu.Trigger>
          <Menu.Portal><Menu.Content className="dropdown-content policy-favorites-menu" align="end" sideOffset={6}>
            <Menu.Label className="policy-favorites-title">常用策略<span>{favorites.length} 项</span></Menu.Label>
            <div className="policy-favorites-search"><Search size={13} /><input aria-label="搜索分类策略" placeholder="搜索策略…" value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key !== "Escape") event.stopPropagation(); }} /></div>
            <div className="policy-favorites-list">{matching.length ? matching.map(policy => {
              const checked = ids.includes(policy.id);
              return <Menu.CheckboxItem className="policy-favorites-item" key={policy.id} checked={checked} title={policy.name}
                onSelect={event => event.preventDefault()} onCheckedChange={value => toggle(policy.id, value)}>
                <span className="policy-favorites-check"><Menu.ItemIndicator><Check size={13} /></Menu.ItemIndicator></span><span className="policy-favorites-name">{policy.name}</span>
              </Menu.CheckboxItem>;
            }) : <p className="policy-shortcuts-empty">没有匹配的策略</p>}</div>
          </Menu.Content></Menu.Portal>
        </Menu.Root>
        <Button variant="ghost" size="sm" onClick={onCreatePolicy}><Plus size={13} />新建策略</Button>
      </div>
    </div>
    {selectedPolicy && Boolean(selectedPolicy.result_count) && <div className="policy-shortcut-tags"><button className="tag" data-active={!selectedTagIds.length} aria-pressed={!selectedTagIds.length} onClick={() => onSelectTag("")}>全部标签</button>{selectedPolicy.tags.map(tag => <button className="tag" data-active={selectedTagIds.includes(tag.id)} aria-pressed={selectedTagIds.includes(tag.id)} key={tag.id} onClick={() => onSelectTag(tag.id)}>{tag.label}</button>)}</div>}
  </>;
}
