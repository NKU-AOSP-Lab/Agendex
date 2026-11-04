import { Select } from "./ui/select";

export function evidenceSourceLabel(kinds: string[]) {
  return kinds.map(kind => ({ metadata: "Meta", readme: "README", source_code: "源码" })[kind] || kind).join(" + ");
}

/** Data sources are chosen for each task, independently of the classification rules. */
export function EvidenceSourceSelect({ kinds, onChange, disabled }: {
  kinds: string[]; onChange: (kinds: string[]) => void; disabled?: boolean;
}) {
  const standard = kinds.length === 2 && kinds.includes("metadata") && kinds.includes("readme");
  return <Select label="数据来源" value={standard ? "metadata_readme" : "legacy"} disabled={disabled}
    onValueChange={value => { if (value === "metadata_readme") onChange?.(["metadata", "readme"]); }} options={[
      { value: "metadata_readme", label: "Meta + README" },
      ...(!standard ? [{ value: "legacy", label: evidenceSourceLabel(kinds), disabled: true }] : []),
      { value: "repository_code", label: "整个仓库（含代码）", disabled: true, hint: "等待实现中" },
    ]} />;
}
