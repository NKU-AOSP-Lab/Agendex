import type { ClassifierModel, Health, ModelOption } from "./api";

export const defaultClassifierModel: ClassifierModel = "typesafe/jev-1.14";
export function classifierLabel(model: string | undefined) {
  if (!model) return "";
  if (model.startsWith("typesafe/jev-")) return "Jev 1.14";
  if (model.toLowerCase().includes("glm-5.3")) return "GLM 5.3";
  return model;
}

export function classifierOptions(health?: Health): ModelOption[] {
  return health?.models || [
    { id: defaultClassifierModel, label: "Jev 1.14", configured: Boolean(health?.jev_configured) },
    { id: "glm-5.3", label: "GLM 5.3", configured: Boolean(health?.glm_configured) },
  ];
}
