/**
 * Display formatting. Pure functions over server values; none of them invent
 * a value - a missing input renders as an explicit "-" / "n/a".
 */

export const DASH = "–";

export function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

/** Seconds -> "850 ms" / "12.3 s" / "4 min 05 s" / "1 h 02 min". */
export function fmtDuration(seconds: number | null | undefined): string {
  if (!isNum(seconds) || seconds < 0) return DASH;
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} min ${String(Math.floor(seconds % 60)).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

/** Duration between two server timestamps; while unfinished, up to `nowS`. */
export function spanSeconds(start: number | null | undefined, end: number | null | undefined, nowS?: number): number | null {
  if (!isNum(start)) return null;
  if (isNum(end)) return Math.max(0, end - start);
  if (isNum(nowS)) return Math.max(0, nowS - start);
  return null;
}

/** Unix seconds -> local "14:03:22" (today) or "Oct 19 14:03". */
export function fmtTime(unixS: number | null | undefined): string {
  if (!isNum(unixS) || unixS <= 0) return DASH;
  const d = new Date(unixS * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  if (sameDay) return time;
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time.slice(0, 5)}`;
}

export function fmtDateTime(unixS: number | null | undefined): string {
  if (!isNum(unixS) || unixS <= 0) return DASH;
  return new Date(unixS * 1000).toLocaleString(undefined, { hour12: false });
}

/** "3 min ago" relative to `nowMs` (defaults to the wall clock). */
export function fmtAgo(unixS: number | null | undefined, nowMs: number = Date.now()): string {
  if (!isNum(unixS) || unixS <= 0) return DASH;
  const s = Math.max(0, nowMs / 1000 - unixS);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (!isNum(n) || n < 0) return DASH;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
}

/** Nanoseconds with sign, 3 decimals (Vivado's own precision). */
export function fmtNs(ns: number | null | undefined): string {
  if (!isNum(ns)) return DASH;
  return `${ns.toFixed(3)} ns`;
}

export function fmtPct(p: number | null | undefined, digits = 1): string {
  if (!isNum(p)) return DASH;
  return `${p.toFixed(digits)}%`;
}

export function fmtNum(x: unknown, digits = 3): string {
  if (!isNum(x)) return x === null || x === undefined ? DASH : String(x);
  if (Number.isInteger(x)) return String(x);
  return String(Number(x.toFixed(digits)));
}

export function shortSha(sha: string | null | undefined, n = 12): string {
  if (!sha) return DASH;
  return sha.length > n ? sha.slice(0, n) + "…" : sha;
}

export function humanize(s: string | null | undefined): string {
  if (!s) return DASH;
  return s.replace(/_/g, " ");
}

export function hex4(n: number | null | undefined): string {
  if (!isNum(n)) return DASH;
  return "0x" + n.toString(16).padStart(4, "0");
}

/** Compact one-line rendering of an arbitrary JSON value for tables. */
export function compactJson(v: unknown, max = 160): string {
  if (v === undefined) return DASH;
  if (typeof v === "string") return v.length > max ? v.slice(0, max) + "…" : v;
  if (isNum(v) || typeof v === "boolean" || v === null) return String(v);
  let s: string;
  try {
    s = JSON.stringify(v);
  } catch {
    s = String(v);
  }
  return s.length > max ? s.slice(0, max) + "…" : s;
}

export function prettyJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export function pluralize(n: number, word: string, plural = word + "s"): string {
  return `${n} ${n === 1 ? word : plural}`;
}
