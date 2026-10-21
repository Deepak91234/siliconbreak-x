/**
 * Scrolling log tail with severity colouring, a text filter and "follow"
 * (auto-scroll while new lines arrive, paused when the user scrolls up).
 * Only the last `maxLines` lines are rendered to keep long Vivado logs cheap.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { cx } from "../components/ui";

function lineClass(l: string): string {
  if (/^(ERROR|FATAL)\b|%Error|\bERROR:/.test(l)) return "text-bad";
  if (/CRITICAL WARNING/.test(l)) return "text-orange";
  if (/^WARNING\b|%Warning|\bWARNING:/.test(l)) return "text-warn";
  if (/^INFO: \[(Synth|Place|Route|Timing|Bitstream|Common 17-206)/.test(l) || /^(Phase|Starting|Finished|write_bitstream completed)/.test(l)) return "text-info";
  return "text-fg/85";
}

export function LogView({ lines, follow = true, height = "h-80", maxLines = 3000, emptyText = "No log lines." }: {
  lines: string[];
  follow?: boolean;
  height?: string;
  maxLines?: number;
  emptyText?: string;
}) {
  const [filter, setFilter] = useState("");
  const [onlyIssues, setOnlyIssues] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [stuck, setStuck] = useState(true);

  const shown = useMemo(() => {
    let ls = lines;
    if (onlyIssues) ls = ls.filter((l) => /ERROR|CRITICAL WARNING|WARNING|%Error|%Warning/.test(l));
    if (filter) {
      const f = filter.toLowerCase();
      ls = ls.filter((l) => l.toLowerCase().includes(f));
    }
    return ls.length > maxLines ? ls.slice(ls.length - maxLines) : ls;
  }, [lines, filter, onlyIssues, maxLines]);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el && follow && stick.current) el.scrollTop = el.scrollHeight;
  }, [shown, follow]);

  useEffect(() => {
    stick.current = true;
    setStuck(true);
  }, [follow]);

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="filter…"
          className="h-6 w-40 rounded border border-line bg-sunken px-2 outline-none focus:border-accent"
        />
        <label className="flex items-center gap-1 text-muted">
          <input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} />
          warnings/errors only
        </label>
        <span className="text-faint">
          {shown.length}{shown.length !== lines.length ? ` of ${lines.length}` : ""} lines
        </span>
        {follow && !stuck && (
          <button
            type="button"
            className="ml-auto text-accent hover:underline"
            onClick={() => {
              stick.current = true;
              setStuck(true);
              if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
            }}
          >
            jump to end
          </button>
        )}
      </div>
      <div
        ref={ref}
        onScroll={() => {
          const el = ref.current;
          if (!el) return;
          const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          stick.current = atEnd;
          if (atEnd !== stuck) setStuck(atEnd);
        }}
        className={cx("overflow-auto rounded-md bg-sunken p-2 font-mono text-[11px] leading-[1.5]", height)}
      >
        {shown.length === 0 ? (
          <div className="text-faint">{emptyText}</div>
        ) : (
          shown.map((l, i) => (
            <div key={i} className={cx("whitespace-pre", lineClass(l))}>
              {l || " "}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
