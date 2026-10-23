import { describe, expect, it } from "vitest";
import { buildBoardModel, inputPort, rowX } from "./model";

describe("Basys 3 board model", () => {
  const model = buildBoardModel(null);
  it("has the board's 16 LEDs, 16 switches and five buttons with guide-compatible ids", () => {
    expect(model.filter((c) => c.kind === "led").map((c) => c.id)).toEqual(Array.from({ length: 16 }, (_, i) => `led${i}`));
    expect(model.filter((c) => c.kind === "switch")).toHaveLength(16);
    expect(model.filter((c) => c.kind === "button").map((c) => c.id).sort()).toEqual(["btnC", "btnD", "btnL", "btnR", "btnU"]);
  });
  it("puts LD0/SW0 on the right and LD15/SW15 on the left, each LED above its switch", () => {
    expect(rowX(0)).toBeGreaterThan(rowX(15));
    const led0 = model.find((c) => c.id === "led0")!;
    const sw0 = model.find((c) => c.id === "sw0")!;
    expect(led0.x).toBe(sw0.x);
    expect(led0.y).toBeLessThan(sw0.y);
  });
  it("arranges the buttons as a plus around BTNC", () => {
    const at = (id: string) => model.find((c) => c.id === id)!;
    expect(at("btnU").y).toBeLessThan(at("btnC").y);
    expect(at("btnD").y).toBeGreaterThan(at("btnC").y);
    expect(at("btnL").x).toBeLessThan(at("btnC").x);
    expect(at("btnR").x).toBeGreaterThan(at("btnC").x);
  });
  it("takes pins only from the backend board definition", () => {
    expect(model.find((c) => c.id === "btnU")!.pin).toBeNull();
    const withPins = buildBoardModel({ id: "basys3", name: "", part: "", idcode: "", clock: { port: "clk", hz: 1 }, iostandard: "",
      ports: { btnU: { direction: "input", kind: "button", pins: ["T18"], active: "high" } } });
    expect(withPins.find((c) => c.id === "btnU")!.pin).toBe("T18");
  });
  it("maps clickable components to virtual-FPGA ports", () => {
    expect(inputPort("sw3")).toEqual({ port: "sw", bit: 3 });
    expect(inputPort("btnC")).toEqual({ port: "btnC" });
    expect(inputPort("led0")).toBeNull();
  });
});
