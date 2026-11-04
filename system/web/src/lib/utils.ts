import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)); }

export function formatNumber(value: number | null | undefined) {
  return value == null ? "—" : value.toLocaleString("zh-CN");
}

export function compactNumber(value: number) {
  return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k` : `${value}`;
}

export function formatDate(value: string | null | undefined, includeTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return new Intl.DateTimeFormat("zh-CN", includeTime
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}
