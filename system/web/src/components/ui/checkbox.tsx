// shadcn/ui Checkbox pattern (MIT), built on the accessible Radix primitive.
import * as React from "react";
import * as Primitive from "@radix-ui/react-checkbox";
import { Check, Minus } from "lucide-react";
import { cn } from "@/lib/utils";

export const Checkbox = React.forwardRef<React.ElementRef<typeof Primitive.Root>, React.ComponentPropsWithoutRef<typeof Primitive.Root>>(
  ({ className, checked, ...props }, ref) => <Primitive.Root ref={ref} checked={checked} className={cn("checkbox", className)} {...props}>
    <Primitive.Indicator className="checkbox-indicator">{checked === "indeterminate" ? <Minus size={11} /> : <Check size={11} strokeWidth={3} />}</Primitive.Indicator>
  </Primitive.Root>,
);
Checkbox.displayName = "Checkbox";
