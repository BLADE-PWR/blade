// Object groups and event kinds: Polish names and the fixed colours (the same as tools/viewer).

export const GROUPS = ["soldier", "tank", "military_vehicle"] as const;
export const EVENT_KINDS = ["explosion", "tank_fire"] as const;

export const NAMES: Record<string, string> = {
  soldier: "żołnierz",
  tank: "czołg",
  military_vehicle: "pojazd",
  explosion: "wybuch",
  tank_fire: "strzał czołgu",
};

export const COLORS: Record<string, string> = {
  soldier: "#3d8bff",
  tank: "#a0dc3c",
  military_vehicle: "#ffc800",
  explosion: "#ff4d4d",
  tank_fire: "#f05cf0",
};

export const nameOf = (grp: string) => NAMES[grp] ?? grp;
export const colorOf = (grp: string) => COLORS[grp] ?? "#c9d1d9";
