import * as Primitive from "@radix-ui/react-progress";
export function Progress({ value, label }: { value: number; label: string }) {
  return <Primitive.Root className="progress" value={value} aria-label={label}>
    <Primitive.Indicator className="progress-indicator" style={{ transform: `translateX(-${100 - value}%)` }} />
  </Primitive.Root>;
}
