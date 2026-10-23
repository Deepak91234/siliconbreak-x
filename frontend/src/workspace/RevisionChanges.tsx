/**
 * "What changed" for a revision versus its parent (or any revision): the request,
 * spec field changes, changed files, simulation / Vivado / verification results
 * side by side. Everything is read from the two stored revisions and the diff API.
 */

import { useMemo } from "react";
import { api } from "../api/client";
import type { Revision } from "../api/types";
import { Card, Empty, Pill } from "../components/ui";
import { buildStatus, revisionKind, verdict } from "../lib/status";
import { useResource } from "../lib/useResource";
import { DiffStatsBadge, DiffView } from "../tabs/DiffView";

type Flat = Record<string, string>;

function flatten(v: unknown, prefix = "", out: Flat = {}): Flat {
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) flatten(x, prefix ? `${prefix}.${k}` : k, out);
  } else out[prefix] = JSON.stringify(v);
  return out;
}

function SpecChanges({ a, b }: { a: Revision; b: Revision }) {
  const rows = useMemo(() => {
    const fa = flatten(a.spec), fb = flatten(b.spec);
    const keys = Array.from(new Set([...Object.keys(fa), ...Object.keys(fb)])).filter((k) => fa[k] !== fb[k] && !k.startsWith("summary") && !k.startsWith("title"));
    return keys.sort().map((k) => ({ k, before: fa[k] ?? "—", after: fb[k] ?? "—" }));
  }, [a, b]);
  if (!rows.length) return <p className="text-xs text-muted">The specification did not change (an automatic repair keeps the spec and changes only the RTL).</p>;
  return (
    <table className="w-full text-xs">
      <thead><tr className="text-left text-faint"><th className="py-1 pr-3 font-normal">Field</th><th className="pr-3 font-normal">Before (r{a.number})</th><th className="font-normal">After (r{b.number})</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.k} className="border-t border-line/60 align-top">
            <td className="py-1 pr-3 font-mono text-muted">{r.k}</td>
            <td className="max-w-[18rem] break-words pr-3 font-mono text-bad/90">{r.before}</td>
            <td className="max-w-[18rem] break-words font-mono text-ok">{r.after}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ResultCompare({ a, b }: { a: Revision; b: Revision }) {
  const sim = (r: Revision) => (r.simulation ? `${r.simulation.passed_checks} passed / ${r.simulation.failed_checks} failed` : "not simulated");
  const timing = (r: Revision) => {
    const t = (r.build as { timing?: { wns_ns?: number; whs_ns?: number } } | null)?.timing;
    return t ? `WNS ${t.wns_ns} ns · WHS ${t.whs_ns} ns` : "—";
  };
  const util = (r: Revision) => {
    const u = (r.build as { utilization?: { summary?: Record<string, { used: number }> } } | null)?.utilization?.summary;
    return u ? `${u.lut?.used ?? "?"} LUT · ${u.ff?.used ?? "?"} FF` : "—";
  };
  const ver = (r: Revision) => (r.verification as { status?: string } | null)?.status ?? null;
  const rows: Array<[string, string, string]> = [
    ["Simulation", sim(a), sim(b)],
    ["Vivado build", buildStatus(a.build_status).label, buildStatus(b.build_status).label],
    ["Timing", timing(a), timing(b)],
    ["Resources", util(a), util(b)],
    ["Webcam verification", ver(a) ? verdict(ver(a)).label : "not verified", ver(b) ? verdict(ver(b)).label : "not verified"],
  ];
  return (
    <table className="w-full text-xs">
      <thead><tr className="text-left text-faint"><th className="py-1 pr-3 font-normal"></th><th className="pr-3 font-normal">r{a.number}</th><th className="font-normal">r{b.number}</th></tr></thead>
      <tbody>
        {rows.map(([k, x, y]) => (
          <tr key={k} className="border-t border-line/60"><td className="py-1 pr-3 text-muted">{k}</td><td className="pr-3">{x}</td><td className={x !== y ? "font-medium text-fg" : ""}>{y}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

export function RevisionChanges({ revisionId }: { revisionId: string | null }) {
  const cur = useResource(revisionId, 0, (sig) => api.revision(revisionId as string, sig));
  const parentId = cur.data?.parent_id ?? null;
  const parent = useResource(parentId, 0, (sig) => api.revision(parentId as string, sig));
  const diff = useResource(revisionId && parentId ? `${revisionId}:${parentId}` : null, 0,
    (sig) => api.revisionDiff(revisionId as string, parentId, sig));
  if (!revisionId || !cur.data) return null;
  if (!parentId) return <Empty title={`r${cur.data.number} is the first revision`}>Comparisons appear after a follow-up request or an automatic repair.</Empty>;
  if (!parent.data) return null;
  const a = parent.data, b = cur.data;
  const changedFiles = (diff.data?.files ?? []).filter((f) => f.diff);
  return (
    <Card title={`What changed: r${b.number} (${revisionKind(b.kind).label}) vs r${a.number}`}>
      <div className="space-y-4">
        <div className="text-sm">
          <span className="text-faint">Request: </span><span className="text-fg">“{b.user_request}”</span>
          {b.kind === "repair" && <Pill tone="violet" className="ml-2">automatic repair</Pill>}
        </div>
        {b.change_summary?.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted">{b.change_summary.map((c, i) => <li key={i}>{c}</li>)}</ul>
        )}
        {b.kind === "repair" && b.explanation && <p className="text-xs text-muted"><span className="text-faint">Why this repair: </span>{b.explanation}</p>}
        <section>
          <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-faint">Behaviour (specification fields)</h4>
          <SpecChanges a={a} b={b} />
        </section>
        <section>
          <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-faint">Results</h4>
          <ResultCompare a={a} b={b} />
        </section>
        <section>
          <h4 className="mb-1 text-2xs font-semibold uppercase tracking-wide text-faint">Changed files</h4>
          {changedFiles.length === 0 ? <p className="text-xs text-muted">No file changes.</p> : (
            <div className="space-y-2">
              {changedFiles.map((f) => (
                <details key={f.name} className="rounded-md border border-line">
                  <summary className="flex cursor-pointer items-center gap-2 px-2.5 py-1.5 text-xs">
                    <span className="font-mono text-fg">{f.name}</span><DiffStatsBadge diff={f.diff} />
                  </summary>
                  <div className="border-t border-line"><DiffView diff={f.diff} mode="unified" lang={f.name.endsWith(".xdc") ? "tcl" : "verilog"} /></div>
                </details>
              ))}
              {diff.data?.spec_diff && (
                <details className="rounded-md border border-line">
                  <summary className="flex cursor-pointer items-center gap-2 px-2.5 py-1.5 text-xs"><span className="font-mono text-fg">spec.json</span><DiffStatsBadge diff={diff.data.spec_diff} /></summary>
                  <div className="border-t border-line"><DiffView diff={diff.data.spec_diff} mode="unified" lang="json" /></div>
                </details>
              )}
            </div>
          )}
        </section>
      </div>
    </Card>
  );
}
