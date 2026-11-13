// Names and colours of object groups and event kinds (the same as tools/viewer).

export const GROUPS = ["soldier", "tank", "military_vehicle"] as const;
export const EVENT_KINDS = ["explosion", "tank_fire"] as const;

const NAMES: Record<string, string> = {
  soldier: "soldier",
  tank: "tank",
  military_vehicle: "vehicle",
  explosion: "explosion",
  tank_fire: "tank fire",
};

const COLORS: Record<string, string> = {
  soldier: "#3d8bff",
  tank: "#a0dc3c",
  military_vehicle: "#ffc800",
  explosion: "#ff4d4d",
  tank_fire: "#f05cf0",
};

export const SEVERITY_COLORS: Record<string, string> = { warning: "#ff9f43", notice: "#9fb3c8", info: "#a3aeba" };

export const nameOf = (grp: string) => NAMES[grp] ?? grp;
export const colorOf = (grp: string) => COLORS[grp] ?? "#c9d1d9";
