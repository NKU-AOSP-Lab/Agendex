import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Keep a usable scrollbar visible on platforms that hide native overlay bars. */
export function HorizontalScroll({ children, label, className }: {
  children: ReactNode; label: string; className?: string;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const [scroll, setScroll] = useState({ position: 0, maximum: 0, thumb: 24 });

  function measure() {
    const element = viewport.current;
    if (!element) return;
    const maximum = Math.max(0, element.scrollWidth - element.clientWidth);
    const position = Math.max(0, Math.min(maximum, element.scrollLeft));
    const thumb = Math.max(24, element.clientWidth ** 2 / Math.max(1, element.scrollWidth));
    setScroll(previous => previous.position === position && previous.maximum === maximum && previous.thumb === thumb
      ? previous : { position, maximum, thumb });
  }

  useLayoutEffect(() => {
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (viewport.current) observer?.observe(viewport.current);
    if (content.current) observer?.observe(content.current);
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
  }, []);

  const overflowing = scroll.maximum > 1;
  return <div className={cn("horizontal-scroll", className)} data-overflow={overflowing}>
    <div ref={viewport} className="horizontal-scroll-viewport" role="region" aria-label={label} tabIndex={0} onScroll={measure}>
      <div ref={content} className="horizontal-scroll-content">{children}</div>
    </div>
    {overflowing && <input className="horizontal-scrollbar" type="range" aria-label={`横向滚动${label}`}
      min={0} max={scroll.maximum} step={1} value={scroll.position}
      style={{ "--scroll-thumb-width": `${scroll.thumb}px` } as CSSProperties}
      onChange={event => { if (viewport.current) { viewport.current.scrollLeft = event.currentTarget.valueAsNumber; measure(); } }} />}
  </div>;
}
