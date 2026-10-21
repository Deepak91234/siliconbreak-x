/**
 * Unified-diff parsing for the revision diff view.
 *
 * The server computes diffs (`/api/revisions/{id}/diff`, Python difflib); the
 * client only lays them out. parseUnifiedDiff keeps line numbers so both the
 * unified and the side-by-side layout can show them, and toSideBySide pairs a
 * run of deletions with the following run of additions (the usual "changed
 * lines" presentation).
 */

export type DiffLineType = "ctx" | "add" | "del" | "hunk" | "meta";

export interface DiffLine {
  type: DiffLineType;
  text: string;
  oldNo: number | null;
  newNo: number | null;
}

export interface DiffStats {
  added: number;
  removed: number;
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

export function parseUnifiedDiff(text: string): DiffLine[] {
  const out: DiffLine[] = [];
  if (!text) return out;
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;
  const lines = text.split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  for (const raw of lines) {
    const line = raw.replace(/\r$/, "");
    const h = HUNK.exec(line);
    if (h) {
      oldNo = parseInt(h[1], 10);
      newNo = parseInt(h[3], 10);
      inHunk = true;
      out.push({ type: "hunk", text: line, oldNo: null, newNo: null });
      continue;
    }
    // Before the first hunk only file headers ("--- a", "+++ b") can appear. Inside a
    // hunk a line such as "--- x" is a deleted line whose text starts with "-- ".
    if (!inHunk) {
      out.push({ type: "meta", text: line, oldNo: null, newNo: null });
      continue;
    }
    if (line.startsWith("\\")) {
      // "\ No newline at end of file"
      out.push({ type: "meta", text: line, oldNo: null, newNo: null });
      continue;
    }
    const tag = line.charAt(0);
    const body = line.slice(1);
    if (tag === "+") {
      out.push({ type: "add", text: body, oldNo: null, newNo: newNo++ });
    } else if (tag === "-") {
      out.push({ type: "del", text: body, oldNo: oldNo++, newNo: null });
    } else {
      out.push({ type: "ctx", text: tag === " " ? body : line, oldNo: oldNo++, newNo: newNo++ });
    }
  }
  return out;
}

export function diffStats(lines: DiffLine[]): DiffStats {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.type === "add") added++;
    else if (l.type === "del") removed++;
  }
  return { added, removed };
}

export interface SideRow {
  kind: "pair" | "hunk" | "meta";
  left: DiffLine | null;
  right: DiffLine | null;
}

export function toSideBySide(lines: DiffLine[]): SideRow[] {
  const rows: SideRow[] = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (l.type === "hunk" || l.type === "meta") {
      rows.push({ kind: l.type, left: l, right: l });
      i++;
      continue;
    }
    if (l.type === "ctx") {
      rows.push({ kind: "pair", left: l, right: l });
      i++;
      continue;
    }
    // A change block: deletions then additions (difflib order), possibly only one side.
    const dels: DiffLine[] = [];
    const adds: DiffLine[] = [];
    while (i < lines.length && lines[i].type === "del") dels.push(lines[i++]);
    while (i < lines.length && lines[i].type === "add") adds.push(lines[i++]);
    const n = Math.max(dels.length, adds.length);
    for (let k = 0; k < n; k++) rows.push({ kind: "pair", left: dels[k] ?? null, right: adds[k] ?? null });
  }
  return rows;
}
