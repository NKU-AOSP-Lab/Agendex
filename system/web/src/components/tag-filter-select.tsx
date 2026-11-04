import * as Menu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown } from "lucide-react";
import type { TagDefinition } from "@/lib/api";
import { Button } from "./ui/button";

export function TagFilterSelect({ tags, selectedIds, onToggle, onClear }: {
  tags: TagDefinition[]; selectedIds: string[]; onToggle: (id: string) => void; onClear: () => void;
}) {
  return <Menu.Root>
    <Menu.Trigger asChild><button type="button" className="select-trigger" aria-label="标签筛选"><span>{selectedIds.length ? `标签 · ${selectedIds.length}` : "全部标签"}</span><span><ChevronDown size={13} /></span></button></Menu.Trigger>
    <Menu.Portal><Menu.Content className="dropdown-content tag-filter-menu" align="end" sideOffset={4}>
      <div className="tag-filter-heading"><Menu.Label>匹配任一标签</Menu.Label><Button type="button" variant="ghost" size="sm" disabled={!selectedIds.length} onClick={onClear}>清空</Button></div>
      {tags.map(tag => <Menu.CheckboxItem key={tag.id} checked={selectedIds.includes(tag.id)} className="tag-filter-item" title={tag.description}
        onSelect={event => event.preventDefault()} onCheckedChange={() => onToggle(tag.id)}>
        <span className="tag-filter-check"><Menu.ItemIndicator><Check size={12} /></Menu.ItemIndicator></span><span>{tag.label}</span>
      </Menu.CheckboxItem>)}
    </Menu.Content></Menu.Portal>
  </Menu.Root>;
}
