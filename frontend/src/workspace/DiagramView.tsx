/**
 * Functional block diagram of a revision (GET /api/revisions/{id}/diagram).
 * Blocks are labelled by origin: "RTL" = an instance parsed from this revision's
 * Verilog, "spec" = a functional block named from the validated spec, "board" =
 * a trusted board resource. It is explicitly not a synthesized netlist.
 * Clicking an input or output block highlights it on the Basys 3 twin.
 */

import { useMemo } from "react";
import { api } from "../api/client";
import type { Diagram, DiagramNode } from "../api/types";
import { Empty, ErrorNote, Loading, cx } from "../components/ui";
import { highlight, setUi, useUi } from "../lib/ui";
import { useResource } from "../lib/useResource";
import { useAppState } from "../store/store";

const COL_X = [20, 230, 450, 690];
const NODE_W = 170;
const NODE_H = 46;
const GAP = 18;

export function DiagramView({ revisionId }: { revisionId: string | null }) {
  const version = useAppState((s) => (revisionId ? s.revisionVersions[revisionId] ?? 0 : 0));
  const res = useResource(revisionId, version, (sig) => api.diagram(revisionId as string, sig));
  if (!revisionId) return <Empty title="No design yet">The block diagram appears once the agent has generated Verilog.</Empty>;
  if (res.loading && !res.data) return <Loading label="Building the diagram…" />;
  if (res.error && !res.data) return res.notFound ? <Empty title="No diagram">This revision has no RTL yet.</Empty> : <ErrorNote error={res.error} onRetry={res.reload} />;
  if (!res.data) return null;
  return <DiagramCanvas d={res.data} />;
}

function DiagramCanvas({ d }: { d: Diagram }) {
  const hl = useUi((s) => s.highlight);
  const layout = useMemo(() => {
    const cols: DiagramNode[][] = [[], [], [], []];
    d.nodes.forEach((n) => cols[Math.min(3, n.column)].push(n));
    const pos = new Map<string, { x: number; y: number }>();
    const heights = cols.map((c) => c.length * (NODE_H + GAP));
    const H = Math.max(240, ...heights) + 40;
    cols.forEach((c, ci) => {
      const top = (H - heights[ci]) / 2 + GAP / 2;
      c.forEach((n, i) => pos.set(n.id, { x: COL_X[ci], y: top + i * (NODE_H + GAP) }));
    });
    return { pos, H };
  }, [d]);

  const linked = (n: DiagramNode) => [n.component, ...(n.components ?? [])].filter(Boolean) as string[];
  const active = (n: DiagramNode) => linked(n).some((id) => hl.includes(id));

  return (
    <div className="space-y-3">
      <p className="text-sm leading-relaxed text-muted">{d.explanation}</p>
      <div className="overflow-x-auto rounded-lg border border-line bg-sunken/50">
        <svg viewBox={`0 0 ${COL_X[3] + NODE_W + 20} ${layout.H}`} className="block h-auto min-w-[640px] w-full" role="img"
             aria-label="Functional block diagram">
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M0 0L10 5L0 10z" className="fill-[rgb(var(--muted))]" />
            </marker>
          </defs>
          {["Board inputs", "Conditioning (wrapper)", "Behaviour core (cl_core)", "Board outputs"].map((t, i) => (
            <text key={t} x={COL_X[i] + NODE_W / 2} y="16" textAnchor="middle" fontSize="11" className="fill-[rgb(var(--faint))]">{t}</text>
          ))}
          {d.edges.map((e, i) => {
            const a = layout.pos.get(e.from);
            const b = layout.pos.get(e.to);
            if (!a || !b) return null;
            const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
            const back = x2 <= x1;
            const path = back
              ? `M${a.x + NODE_W / 2} ${a.y + NODE_H} C${a.x + NODE_W / 2} ${a.y + NODE_H + 20}, ${b.x + NODE_W / 2} ${b.y - 20}, ${b.x + NODE_W / 2} ${b.y}`
              : `M${x1} ${y1} C${x1 + 50} ${y1}, ${x2 - 50} ${y2}, ${x2} ${y2}`;
            return (
              <g key={i}>
                <path d={path} fill="none" markerEnd="url(#arrow)"
                      className={e.clock ? "stroke-[rgb(var(--line-strong))]" : "stroke-[rgb(var(--muted))]"}
                      strokeWidth={e.clock ? 0.8 : 1.3} strokeDasharray={e.clock ? "3 4" : undefined} opacity={e.clock ? 0.6 : 0.9} />
                {e.label && !e.clock && !back && (
                  <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 4} textAnchor="middle" fontSize="9.5" className="fill-[rgb(var(--faint))]">{e.label}</text>
                )}
              </g>
            );
          })}
          {d.nodes.map((n) => {
            const p = layout.pos.get(n.id)!;
            const ids = linked(n);
            const on = active(n);
            return (
              <g key={n.id} transform={`translate(${p.x},${p.y})`} className={ids.length ? "cursor-pointer" : undefined}
                 onClick={() => { if (ids.length) { highlight(on ? [] : ids, "diagram"); setUi({ inspectorOpen: true }); } }}
                 role={ids.length ? "button" : undefined} aria-label={ids.length ? `Highlight ${n.label} on the board` : undefined}>
                <rect width={NODE_W} height={NODE_H} rx="8" strokeWidth={on ? 2 : 1}
                      className={cx(
                        n.kind === "functional" ? "fill-[rgb(var(--raised))] stroke-[rgb(var(--violet)/0.6)]" :
                          n.kind === "rtl" ? "fill-[rgb(var(--panel))] stroke-[rgb(var(--accent)/0.55)]" :
                            "fill-[rgb(var(--panel))] stroke-[rgb(var(--line-strong))]",
                        on && "stroke-[rgb(var(--accent))]",
                      )}
                      strokeDasharray={n.kind === "functional" ? "4 3" : undefined} />
                <text x="12" y="19" fontSize="12.5" fontWeight="600" className="fill-[rgb(var(--fg))]">{n.label}</text>
                <text x="12" y="35" fontSize="10" className="fill-[rgb(var(--faint))] font-mono">{n.sub.length > 26 ? n.sub.slice(0, 25) + "…" : n.sub}</text>
                <text x={NODE_W - 10} y="19" fontSize="9" textAnchor="end"
                      className={n.source === "rtl" ? "fill-[rgb(var(--accent))]" : n.source === "spec" ? "fill-[rgb(var(--violet))]" : "fill-[rgb(var(--faint))]"}>
                  {n.source === "rtl" ? "RTL" : n.source === "spec" ? "spec" : "board"}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <div className="flex flex-wrap items-center gap-4 text-2xs text-faint">
        <span><span className="mr-1 inline-block h-2 w-3 rounded-sm border border-accent/60 align-middle" />RTL instance (parsed from this revision)</span>
        <span><span className="mr-1 inline-block h-2 w-3 rounded-sm border border-dashed border-violet/60 align-middle" />functional block (from the spec)</span>
        <span><span className="mr-1 inline-block w-4 border-t border-dashed border-line-strong align-middle" />100 MHz clock</span>
        <span>Click an input or output block to highlight it on the board.</span>
      </div>
      <p className="text-2xs text-faint">{d.label}</p>
    </div>
  );
}
