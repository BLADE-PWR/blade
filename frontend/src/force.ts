// Where forces are concentrated: how much each class counts (the operator sets it), the strength of a group.
import type { Cluster } from "./contracts";

export type Weights = Record<string, number>;

export const MAX_WEIGHT = 5;
export const DEFAULT_WEIGHTS: Weights = { tank: 3, military_vehicle: 2, soldier: 1 };

const KEY = "blade.weights";

export function loadWeights(): Weights {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? "null") as Weights | null;
    if (saved && Object.values(saved).every((w) => typeof w === "number")) return { ...DEFAULT_WEIGHTS, ...saved };
  } catch {
    // no storage, or not ours: the defaults do
  }
  return DEFAULT_WEIGHTS;
}

export function saveWeights(w: Weights): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(w));
  } catch {
    // kept for this tab only
  }
}

export const weightOf = (w: Weights, grp: string) => w[grp] ?? 1;

/** Sum of the weights of a group's members. */
export const strength = (c: Cluster, w: Weights) =>
  Object.entries(c.composition).reduce((sum, [grp, n]) => sum + n * weightOf(w, grp), 0);
