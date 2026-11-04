import type { ReactNode } from "react";
import { AlertCircle, ArrowUpRight, Inbox, Loader2, RefreshCw } from "lucide-react";
import { Button } from "./ui/button";

export function Loading({ label = "正在加载" }: { label?: string }) {
  return <div className="state-panel" role="status"><Loader2 className="spin" size={20} /><span>{label}</span></div>;
}
export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div className="state-panel error-state" role="alert"><AlertCircle size={22} /><p>{message}</p><Button variant="outline" size="sm" onClick={onRetry}><RefreshCw size={14} />重试</Button></div>;
}
export function EmptyState({ title, description, children, icon }: { title: string; description?: string; children?: ReactNode; icon?: ReactNode }) {
  return <div className="state-panel empty-state"><div className="empty-icon">{icon || <Inbox size={24} strokeWidth={1.5} />}</div><h3>{title}</h3>{description && <p>{description}</p>}{children}</div>;
}
export function ExternalLink({ href, children, className = "" }: { href: string; children: ReactNode; className?: string }) {
  return <a className={`external-link ${className}`} href={href} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={13} /></a>;
}
export function InlineError({ message }: { message: string | null }) {
  return message ? <div className="inline-error" role="alert"><AlertCircle size={15} />{message}</div> : null;
}
