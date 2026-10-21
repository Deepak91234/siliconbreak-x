/** Unified or side-by-side rendering of one server-computed unified diff. */

import { useMemo } from "react";
import { diffStats, parseUnifiedDiff, toSideBySide, type DiffLine } from "../lib/diff";
import { highlight, type CodeLanguage } from "../lib/highlight";
import { cx } from "../components/ui";

export type DiffMode = "unified" | "split";

const ROW_BG: Record<string, string> = {
  add: "bg-ok/10",
  del: "bg-bad/10",
  ctx: "",
  hunk: "bg-info/10 text-info",
  meta: "text-faint",
};

function Code({ line, lang }: { line: DiffLine; lang: CodeLanguage }) {
  if (line.type === "hunk" || line.type === "meta") return <span>{line.text}</span>;
  return <span className="hljs" dangerouslySetInnerHTML={{ __html: highlight(line.text, lang) || "&nbsp;" }} />;
}

function Gutter({ n }: { n: number | null }) {
  return <td className="w-10 select-none border-r border-line/60 px-1.5 text-right align-top text-faint">{n ?? ""}</td>;
}

export function DiffView({ diff, mode, lang }: { diff: string; mode: DiffMode; lang: CodeLanguage }) {
  const lines = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const rows = useMemo(() => (mode === "split" ? toSideBySide(lines) : []), [lines, mode]);
  if (!lines.length) return <div className="px-3 py-2 text-xs text-muted">No differences.</div>;

  if (mode === "unified") {
    return (
      <div className="overflow-x-auto">
        <table className="w-full border-collapse font-mono text-[11.5px] leading-[1.55]">
          <tbody>
            {lines.map((l, i) => (
              <tr key={i} className={ROW_BG[l.type]}>
                <Gutter n={l.oldNo} />
                <Gutter n={l.newNo} />
                <td className={cx("w-4 select-none pl-1 align-top", l.type === "add" ? "text-ok" : l.type === "del" ? "text-bad" : "text-faint")}>
                  {l.type === "add" ? "+" : l.type === "del" ? "-" : ""}
                </td>
                <td className="whitespace-pre pr-4">
                  <Code line={l} lang={lang} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full table-fixed border-collapse font-mono text-[11.5px] leading-[1.55]">
        <colgroup>
          <col className="w-10" />
          <col />
          <col className="w-10" />
          <col />
        </colgroup>
        <tbody>
          {rows.map((r, i) => {
            if (r.kind !== "pair") {
              return (
                <tr key={i} className={ROW_BG[r.kind]}>
                  <td colSpan={4} className="whitespace-pre px-2">{r.left?.text}</td>
                </tr>
              );
            }
            const changed = r.left?.type !== "ctx" || r.right?.type !== "ctx";
            return (
              <tr key={i}>
                <Gutter n={r.left?.oldNo ?? null} />
                <td className={cx("overflow-hidden whitespace-pre pr-2 align-top", r.left ? (changed ? ROW_BG.del : "") : "bg-sunken/70")}>
                  {r.left && <Code line={r.left} lang={lang} />}
                </td>
                <Gutter n={r.right?.newNo ?? null} />
                <td className={cx("overflow-hidden whitespace-pre pr-2 align-top", r.right ? (changed ? ROW_BG.add : "") : "bg-sunken/70")}>
                  {r.right && <Code line={r.right} lang={lang} />}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function DiffStatsBadge({ diff }: { diff: string }) {
  const s = useMemo(() => diffStats(parseUnifiedDiff(diff)), [diff]);
  if (!s.added && !s.removed) return <span className="text-[11px] text-faint">unchanged</span>;
  return (
    <span className="font-mono text-[11px]">
      <span className="text-ok">+{s.added}</span> <span className="text-bad">-{s.removed}</span>
    </span>
  );
}
