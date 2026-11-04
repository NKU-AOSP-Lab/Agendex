import { useEffect, useState, type DependencyList } from "react";

export function useResource<T>(loader: (signal: AbortSignal) => Promise<T>, dependencies: DependencyList) {
  const [data, setData] = useState<T>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null);
    loader(controller.signal).then(result => {
      if (!controller.signal.aborted) setData(result);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "请求失败，请稍后重试");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [...dependencies, generation]);
  return { data, loading, error, reload: () => setGeneration(value => value + 1) };
}
