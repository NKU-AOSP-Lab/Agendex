// shadcn/ui Select composition (MIT), with Radix keyboard and focus behavior.
import * as Primitive from "@radix-ui/react-select";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Check, ChevronDown } from "lucide-react";

export interface SelectOption { value: string; label: string; disabled?: boolean; hint?: string }
export function Select({ value, onValueChange, options, placeholder, label, disabled, className = "" }: {
  value: string; onValueChange: (value: string) => void; options: SelectOption[];
  placeholder?: string; label: string; disabled?: boolean; className?: string;
}) {
  return <Primitive.Root value={value || "__all__"} onValueChange={next => onValueChange(next === "__all__" ? "" : next)} disabled={disabled}>
    <Primitive.Trigger className={`select-trigger ${className}`} aria-label={label}>
      <Primitive.Value placeholder={placeholder} /><Primitive.Icon><ChevronDown size={14} /></Primitive.Icon>
    </Primitive.Trigger>
    <Primitive.Portal><Primitive.Content className="select-content" position="popper" sideOffset={5}>
      <Primitive.Viewport>{options.map(option => {
        const item = <Primitive.Item className="select-item" value={option.value || "__all__"} disabled={option.disabled}>
          <Primitive.ItemText>{option.label}</Primitive.ItemText><Primitive.ItemIndicator><Check size={14} /></Primitive.ItemIndicator>
        </Primitive.Item>;
        return option.disabled && option.hint ? <Tooltip.Provider key={option.value} delayDuration={150}><Tooltip.Root>
          <Tooltip.Trigger asChild><span className="select-disabled-option" tabIndex={0} aria-label={`${option.label}：${option.hint}`}>{item}</span></Tooltip.Trigger>
          <Tooltip.Portal><Tooltip.Content className="select-tooltip" side="left" sideOffset={8}>{option.hint}</Tooltip.Content></Tooltip.Portal>
        </Tooltip.Root></Tooltip.Provider> : <span className="select-option" key={option.value || "__all__"}>{item}</span>;
      })}</Primitive.Viewport>
    </Primitive.Content></Primitive.Portal>
  </Primitive.Root>;
}
