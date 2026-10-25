/** Source excerpt of a finding with the flagged lines highlighted (Security Lab). */

import { useMemo } from "react";
import type { Finding } from "../../api/security";
import { cx } from "../../components/ui";
import { highlightLines } from "../../lib/highlight";

export function SourceExcerpt({ f, text, context = 3 }: { f: Finding; text: string | undefined; context?: number }) {
  const lines = useMemo(() => (text ? highlightLines(text, "verilog") : null), [text]);
  const lo = Math.max(1, f.line_start - context);
  const hi = Math.min(lines?.length ?? f.line_end, f.line_end + context);
  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-sunken py-2 font-mono text-[11.5px] leading-[1.6]">
      {lines ? lines.slice(lo - 1, hi).map((html, i) => {
        const n = lo + i;
        const on = n >= f.line_start && n <= f.line_end;
        return (
          <div key={n} className={cx("flex", on && "bg-bad/15")}>
            <span className={cx("w-10 shrink-0 select-none pr-3 text-right", on ? "text-bad" : "text-faint")}>{n}</span>
            <span className="hljs whitespace-pre pr-3" dangerouslySetInnerHTML={{ __html: html || "&nbsp;" }} />
          </div>
        );
      }) : <div className="px-3 text-2xs text-faint">Loading source…</div>}
    </div>
  );
}
