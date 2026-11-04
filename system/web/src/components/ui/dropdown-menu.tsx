import * as Primitive from "@radix-ui/react-dropdown-menu";
import { MoreHorizontal } from "lucide-react";
import { Button } from "./button";

export function ActionMenu({ label, items }: { label: string; items: { label: string; action: () => void; destructive?: boolean }[] }) {
  return <Primitive.Root>
    <Primitive.Trigger asChild><Button variant="ghost" size="icon" aria-label={label}><MoreHorizontal size={17} /></Button></Primitive.Trigger>
    <Primitive.Portal><Primitive.Content className="dropdown-content" align="end" sideOffset={4}>
      {items.map(item => <Primitive.Item key={item.label} className={`dropdown-item ${item.destructive ? "text-danger" : ""}`} onSelect={item.action}>{item.label}</Primitive.Item>)}
    </Primitive.Content></Primitive.Portal>
  </Primitive.Root>;
}
