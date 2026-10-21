import { describe, expect, it } from "vitest";
import { faultsForFamily } from "./faults";

const F = [
  { name: "stuck_led", description: "", families: [] },
  { name: "wrong_rate", description: "", families: ["blinker", "counter"] },
  { name: "skip_state", description: "", families: ["traffic_light"] },
];

describe("faultsForFamily", () => {
  it("keeps null while the catalogue is loading", () => {
    expect(faultsForFamily(null, "counter")).toBeNull();
  });
  it("offers every fault when the family is unknown", () => {
    expect(faultsForFamily(F, null)).toBe(F);
  });
  it("filters by family and keeps family-agnostic faults", () => {
    expect(faultsForFamily(F, "counter")!.map((f) => f.name)).toEqual(["stuck_led", "wrong_rate"]);
    expect(faultsForFamily(F, "traffic_light")!.map((f) => f.name)).toEqual(["stuck_led", "skip_state"]);
    expect(faultsForFamily(F, "led_pattern")!.map((f) => f.name)).toEqual(["stuck_led"]);
  });
});
