// shadcn/ui Dialog composition (MIT); Radix supplies focus trapping and dismissal.
import * as React from "react";
import * as Primitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export const Dialog = Primitive.Root;
export const DialogTitle = Primitive.Title;
export const DialogDescription = Primitive.Description;
export interface DialogContentProps extends React.ComponentPropsWithoutRef<typeof Primitive.Content> { drawer?: boolean; hideClose?: boolean }
export const DialogContent = React.forwardRef<React.ElementRef<typeof Primitive.Content>, DialogContentProps>(
  ({ className, children, drawer = false, hideClose = false, ...props }, ref) => <Primitive.Portal>
    <Primitive.Overlay className="dialog-overlay" />
    <Primitive.Content ref={ref} className={cn(drawer ? "drawer-content" : "dialog-content", className)} {...props}>
      {children}
      {!hideClose && <Primitive.Close className="dialog-close" aria-label="关闭"><X size={18} /></Primitive.Close>}
    </Primitive.Content>
  </Primitive.Portal>,
);
DialogContent.displayName = "DialogContent";
