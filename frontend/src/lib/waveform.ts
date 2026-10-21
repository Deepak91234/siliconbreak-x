/**
 * Helpers for the waveform viewer over `vcd_to_json` output (contracts §A):
 * `{"time_unit": "half_cycle", "start", "end", "signals": [{name, width, changes: [[t, value], ...]}]}`.
 *
 * Values are kept as the server's strings ("0", "1", "0x3", "x", "z", "b0101");
 * we only parse them for display and never interpolate between samples.
 */

import type { WaveSignal } from "../api/types";

export interface Segment {
  t0: number;
  t1: number;
  value: string | null; // null = no sample at or before t0 (unknown)
}

/** Index of the last change with time <= t, or -1. Changes are sorted by time. */
export function lastChangeIndex(changes: Array<[number, string]>, t: number): number {
  let lo = 0;
  let hi = changes.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (changes[mid][0] <= t) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

export function valueAt(changes: Array<[number, string]>, t: number): string | null {
  const i = lastChangeIndex(changes, t);
  return i < 0 ? null : changes[i][1];
}

/** Constant-value segments covering [start, end]. */
export function segments(changes: Array<[number, string]>, start: number, end: number): Segment[] {
  if (end <= start) return [];
  const out: Segment[] = [];
  let i = lastChangeIndex(changes, start);
  let t = start;
  let v: string | null = i >= 0 ? changes[i][1] : null;
  i += 1;
  while (i < changes.length && changes[i][0] <= end) {
    const [ct, cv] = changes[i];
    if (ct > t) out.push({ t0: t, t1: ct, value: v });
    t = Math.max(t, ct);
    v = cv;
    i++;
  }
  out.push({ t0: t, t1: end, value: v });
  return out;
}

/** Parse a VCD-JSON value to a BigInt when it is fully known. */
export function parseValue(v: string | null): bigint | null {
  if (v === null) return null;
  const s = v.trim().toLowerCase();
  try {
    if (s.startsWith("0x")) return /^[0-9a-f]+$/.test(s.slice(2)) ? BigInt(s) : null;
    if (s.startsWith("0b")) return /^[01]+$/.test(s.slice(2)) ? BigInt(s) : null;
    if (s.startsWith("b")) return /^[01]+$/.test(s.slice(1)) ? BigInt("0b" + s.slice(1)) : null;
    if (/^[01]+$/.test(s)) return BigInt("0b" + s);
    if (/^\d+$/.test(s)) return BigInt(s);
  } catch {
    return null;
  }
  return null;
}

export type Radix = "hex" | "bin" | "dec";

export function formatValue(v: string | null, width: number, radix: Radix): string {
  if (v === null) return "?";
  const n = parseValue(v);
  if (n === null) return v; // x / z / partially unknown: show verbatim
  if (width <= 1) return n.toString();
  if (radix === "dec") return n.toString(10);
  if (radix === "bin") return n.toString(2).padStart(width, "0");
  return "0x" + n.toString(16).padStart(Math.ceil(width / 4), "0");
}

/** Single-bit level: 1, 0, or null when unknown/x/z. */
export function bitLevel(v: string | null): 0 | 1 | null {
  const n = parseValue(v);
  if (n === null) return null;
  return n === 0n ? 0 : 1;
}

/** Strip the testbench hierarchy prefix for display ("TOP.basys3_top.u_core.value" -> "u_core.value"). */
export function shortSignalName(name: string): string {
  return name.replace(/^TOP\./, "").replace(/^basys3_top\./, "");
}

/** Signals worth showing by default: the board outputs and inputs first, then the core. */
export function defaultSignalOrder(signals: WaveSignal[]): WaveSignal[] {
  const rank = (s: WaveSignal) => {
    const n = shortSignalName(s.name);
    if (/^(clk)$/.test(n)) return 0;
    if (/^(led)(\b|$)/.test(n)) return 1;
    if (/^(sw|btn[CULRD])(\b|$)/.test(n)) return 2;
    if (n.startsWith("u_core.")) return 3;
    return 4;
  };
  return signals.slice().sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

/** "Nice" tick positions (1/2/5 x 10^k) for an axis spanning [start, end]. */
export function niceTicks(start: number, end: number, target = 8): number[] {
  const span = end - start;
  if (!(span > 0)) return [];
  const raw = span / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3.5 ? 2 : norm < 7.5 ? 5 : 10) * mag;
  const out: number[] = [];
  for (let t = Math.ceil(start / step) * step; t <= end + 1e-9; t += step) out.push(Math.round(t * 1e6) / 1e6);
  return out;
}

/** time_unit "half_cycle": two VCD ticks per clock cycle. Other units are shown raw. */
export function ticksPerCycle(timeUnit: string | undefined): number {
  return timeUnit === "half_cycle" ? 2 : 1;
}

export function clampView(start: number, end: number, min: number, max: number, minSpan = 4): [number, number] {
  let span = Math.max(minSpan, end - start);
  span = Math.min(span, Math.max(minSpan, max - min));
  let s = start;
  if (s < min) s = min;
  if (s + span > max) s = Math.max(min, max - span);
  return [s, s + span];
}
