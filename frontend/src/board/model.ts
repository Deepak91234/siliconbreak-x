/**
 * Shared Basys 3 board model for the 2D (SVG) and 3D (three.js) views.
 *
 * Geometry is in millimetres, top-down, origin at the board's top-left corner.
 * The arrangement follows the Basys 3 Rev B: 16 slide switches along the
 * bottom edge (SW15 leftmost ... SW0 rightmost), one LED above each switch
 * (LD15 ... LD0), the four-digit seven-segment display above the left half of
 * the LED row, the five pushbuttons in a plus shape at the lower right, the
 * Artix-7 in the middle, and the USB/power area at the top left. Positions are
 * an accurate-topology approximation of the board drawing, not CAD data.
 *
 * Component ids match backend/specifications/guide.py (sw0..sw15, led0..led15,
 * btnC/btnU/btnL/btnR/btnD). Package pins are never hard-coded here: they are
 * merged in from GET /api/board (boards/basys3/board.json).
 */

import type { BoardDef } from "../api/types";

export type ComponentKind = "led" | "switch" | "button" | "seg" | "fpga" | "connector" | "label";

export interface BoardComponent {
  id: string;
  kind: ComponentKind;
  label: string;
  x: number; // centre, mm
  y: number;
  w: number;
  h: number;
  /** Board port and bit for pins / signals (led, sw, btnU, seg...). */
  port?: string;
  index?: number;
  pin?: string | null;
  height3d: number; // mm above the PCB, for the 3D view
}

export const BOARD_W = 120;
export const BOARD_H = 82;
const ROW_LEFT = 14; // x of SW15 / LD15
const ROW_RIGHT = 106; // x of SW0 / LD0
const PITCH = (ROW_RIGHT - ROW_LEFT) / 15;

export function rowX(index: number): number {
  return ROW_RIGHT - index * PITCH; // index 0 is the rightmost
}

export const BUTTONS: Array<{ id: "btnU" | "btnL" | "btnC" | "btnR" | "btnD"; label: string; dx: number; dy: number }> = [
  { id: "btnU", label: "BTNU", dx: 0, dy: -8 },
  { id: "btnL", label: "BTNL", dx: -8, dy: 0 },
  { id: "btnC", label: "BTNC", dx: 0, dy: 0 },
  { id: "btnR", label: "BTNR", dx: 8, dy: 0 },
  { id: "btnD", label: "BTND", dx: 0, dy: 8 },
];
const BTN_CX = 96;
const BTN_CY = 44;

export function buildBoardModel(def: BoardDef | null): BoardComponent[] {
  const pin = (port: string, index = 0): string | null => def?.ports[port]?.pins[index] ?? null;
  const out: BoardComponent[] = [];
  for (let i = 0; i < 16; i++) {
    out.push({ id: `led${i}`, kind: "led", label: `LD${i}`, x: rowX(i), y: 62.5, w: 2.6, h: 1.6, port: "led", index: i, pin: pin("led", i), height3d: 0.8 });
    out.push({ id: `sw${i}`, kind: "switch", label: `SW${i}`, x: rowX(i), y: 72.5, w: 3.6, h: 7.4, port: "sw", index: i, pin: pin("sw", i), height3d: 3.2 });
  }
  for (const b of BUTTONS) {
    out.push({ id: b.id, kind: "button", label: b.label, x: BTN_CX + b.dx, y: BTN_CY + b.dy, w: 5.6, h: 5.6, port: b.id, pin: pin(b.id), height3d: 3.6 });
  }
  out.push({ id: "seg", kind: "seg", label: "7-segment display", x: 40, y: 47, w: 30, h: 11, port: "seg", height3d: 5 });
  out.push({ id: "fpga", kind: "fpga", label: "Artix-7 XC7A35T", x: 60, y: 27, w: 17, h: 17, height3d: 1.6 });
  out.push({ id: "usb", kind: "connector", label: "USB (PROG/UART)", x: 4.5, y: 14, w: 7, h: 8, height3d: 3 });
  out.push({ id: "power", kind: "connector", label: "POWER", x: 6, y: 4.5, w: 6, h: 4, height3d: 3 });
  out.push({ id: "vga", kind: "connector", label: "VGA", x: 34, y: 4, w: 30, h: 7, height3d: 9 });
  out.push({ id: "usbhost", kind: "connector", label: "USB HOST", x: 86, y: 4.5, w: 14, h: 8, height3d: 7 });
  out.push({ id: "ja", kind: "connector", label: "JA", x: 4, y: 40, w: 6, h: 16, height3d: 8 });
  out.push({ id: "jxadc", kind: "connector", label: "JXADC", x: 4, y: 58, w: 6, h: 14, height3d: 8 });
  out.push({ id: "jb", kind: "connector", label: "JB", x: 116, y: 24, w: 6, h: 16, height3d: 8 });
  out.push({ id: "jc", kind: "connector", label: "JC", x: 116, y: 44, w: 6, h: 16, height3d: 8 });
  return out;
}

/** Board port + value for a clickable input component (virtual FPGA). */
export function inputPort(id: string): { port: "sw" | "btnC" | "btnU" | "btnL" | "btnR" | "btnD"; bit?: number } | null {
  if (/^sw\d+$/.test(id)) return { port: "sw", bit: Number(id.slice(2)) };
  if (id === "btnC" || id === "btnU" || id === "btnL" || id === "btnR" || id === "btnD") return { port: id };
  return null;
}

// ------------------------------------------------------------------ visual state

/** Where a displayed value comes from. Simulated and observed values are never mixed. */
export type Provenance = "simulated" | "observed" | "expected" | "preview";

export interface BoardVisualState {
  /** LED on/off by index; null = unknown (rendered dim with no claim). */
  leds: Record<number, boolean | null>;
  ledSource: Provenance | null;
  switches: Record<number, boolean>;
  buttons: Record<string, boolean>;
  /** Components used by the design (from the guide). */
  used: Set<string>;
  /** Components the current step / hover / diagram points at. */
  focus: Set<string>;
  interactive: boolean;
}

export function emptyVisual(): BoardVisualState {
  return { leds: {}, ledSource: null, switches: {}, buttons: {}, used: new Set(), focus: new Set(), interactive: false };
}

export const PROVENANCE_LABEL: Record<Provenance, string> = {
  simulated: "Simulated (Verilator)",
  observed: "Observed (webcam)",
  expected: "Expected (from spec)",
  preview: "Preview (example data)",
};
