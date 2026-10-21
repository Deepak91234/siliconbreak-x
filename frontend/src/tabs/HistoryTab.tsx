/**
 * History: projects (GET /api/projects), the selected project's revisions and
 * runs (GET /api/projects/{id}), and a compare picker that opens the Verilog
 * tab's diff (GET /api/revisions/{id}/diff?against=).
 */

import { useMemo, useState } from "react";
import type { RevisionSummary, Run } from "../api/types";
import { Button, Card, Empty, ErrorNote, Loading, Pill, cx } from "../components/ui";
import { fmtAgo, fmtDateTime, fmtDuration, spanSeconds } from "../lib/format";
import { buildStatus, revisionKind, revisionStatus, runStatus, simReportStatus } from "../lib/status";
import { effectiveRevisionId, effectiveRunId } from "../store/reducer";
import { useAppState, useStore } from "../store/store";

function ProjectList() {
  const store = useStore();
  const projects = useAppState((s) => s.projects);
  const error = useAppState((s) => s.projectsError);
  const selected = useAppState((s) => s.selection.projectId);

  let body;
  if (projects === null && error) body = <ErrorNote error={error} onRetry={() => void store.loadProjects()} />;
  else if (projects === null) body = <Loading label="Loading projects…" />;
  else if (projects.length === 0) body = <Empty title="No projects yet">Describe a design in the chat to create the first project.</Empty>;
  else
    body = (
      <ul className="-mx-1 space-y-0.5">
        {projects.map((p) => {
          const st = p.latest_run_status ? runStatus(p.latest_run_status) : null;
          return (
            <li key={p.id}>
              <button
                type="button"
                onClick={() => store.selectProject(p.id)}
                className={cx(
                  "w-full rounded-md px-2 py-1.5 text-left transition-colors",
                  p.id === selected ? "bg-accent/10 ring-1 ring-inset ring-accent/40" : "hover:bg-raised",
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-sm">{p.title || p.id}</span>
                  {st && <Pill tone={st.tone}>{st.label}</Pill>}
                </div>
                <div className="mt-0.5 text-[11px] text-faint">
                  {p.revision_count} revision{p.revision_count === 1 ? "" : "s"} · updated {fmtAgo(p.updated_at)}
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    );

  return (
    <Card title="Projects" className="h-fit" actions={error && projects ? <span className="text-[11px] text-bad" title={error}>refresh failed</span> : null}>
      {body}
    </Card>
  );
}

function RevisionTable({ revisions }: { revisions: RevisionSummary[] }) {
  const store = useStore();
  const shown = useAppState(effectiveRevisionId);
  const [base, setBase] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const rows = useMemo(() => [...revisions].sort((a, b) => b.number - a.number), [revisions]);
  const byId = useMemo(() => new Map(revisions.map((r) => [r.id, r])), [revisions]);

  const open = (id: string) => {
    store.selectRevision(id);
    store.setTab("verilog");
  };
  const canCompare = !!base && !!target && base !== target;
  const label = (id: string | null) => (id && byId.get(id) ? `r${byId.get(id)!.number}` : "–");

  return (
    <Card
      title={`Revisions (${revisions.length})`}
      actions={
        <div className="flex items-center gap-2 text-[11px] text-muted">
          <span>
            base <span className="font-mono text-fg">{label(base)}</span> → target <span className="font-mono text-fg">{label(target)}</span>
          </span>
          <Button size="sm" variant="primary" disabled={!canCompare} onClick={() => canCompare && store.compare(base!, target!)}>
            Compare
          </Button>
        </div>
      }
      bodyClassName="p-0"
    >
      {rows.length === 0 ? (
        <div className="p-3">
          <Empty title="No revisions yet">A revision is recorded when the agent generates RTL for this project.</Empty>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-line text-[11px] uppercase tracking-wide text-faint">
              <tr>
                <th className="px-3 py-2 font-medium" title="Compare base">A</th>
                <th className="px-1 py-2 font-medium" title="Compare target">B</th>
                <th className="px-2 py-2 font-medium">Rev</th>
                <th className="px-2 py-2 font-medium">Kind</th>
                <th className="px-2 py-2 font-medium">Status</th>
                <th className="px-2 py-2 font-medium">Sim</th>
                <th className="px-2 py-2 font-medium">Build</th>
                <th className="px-2 py-2 font-medium">Request / changes</th>
                <th className="px-2 py-2 font-medium">Created</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((r) => {
                const k = revisionKind(r.kind);
                const st = revisionStatus(r.status);
                const sim = r.sim_status ? simReportStatus(r.sim_status) : null;
                const b = r.build_status ? buildStatus(r.build_status) : null;
                return (
                  <tr key={r.id} className={cx("align-top", r.id === shown && "bg-accent/5")}>
                    <td className="px-3 py-2">
                      <input type="radio" name="cmp-base" aria-label={`Compare base r${r.number}`} checked={base === r.id} onChange={() => setBase(r.id)} />
                    </td>
                    <td className="px-1 py-2">
                      <input type="radio" name="cmp-target" aria-label={`Compare target r${r.number}`} checked={target === r.id} onChange={() => setTarget(r.id)} />
                    </td>
                    <td className="px-2 py-2 font-mono">
                      r{r.number}
                      {r.id === shown && <span className="ml-1 text-[10px] text-accent">shown</span>}
                    </td>
                    <td className="px-2 py-2"><Pill tone={k.tone}>{k.label}</Pill></td>
                    <td className="px-2 py-2"><Pill tone={st.tone}>{st.label}</Pill></td>
                    <td className="px-2 py-2">{sim ? <Pill tone={sim.tone}>{sim.label}</Pill> : <span className="text-faint">not run</span>}</td>
                    <td className="px-2 py-2">{b ? <Pill tone={b.tone}>{b.label}</Pill> : <span className="text-faint">not run</span>}</td>
                    <td className="max-w-[28rem] px-2 py-2">
                      <div className="line-clamp-2 text-fg" title={r.user_request}>{r.user_request || <span className="text-faint">(no request text)</span>}</div>
                      {r.change_summary?.length > 0 && (
                        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-muted">
                          {r.change_summary.slice(0, 4).map((c, i) => <li key={i}>{c}</li>)}
                          {r.change_summary.length > 4 && <li className="text-faint">+{r.change_summary.length - 4} more</li>}
                        </ul>
                      )}
                      <div className="mt-1 text-[10px] text-faint">
                        generator {r.generator || "–"} · interpreter {r.interpreter || "–"}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-muted" title={fmtDateTime(r.created_at)}>{fmtAgo(r.created_at)}</td>
                    <td className="whitespace-nowrap px-3 py-2 text-right">
                      <div className="flex justify-end gap-1">
                        <Button size="sm" variant="ghost" onClick={() => open(r.id)}>View</Button>
                        {r.parent_id && byId.has(r.parent_id) && (
                          <Button size="sm" variant="ghost" onClick={() => store.compare(r.parent_id!, r.id)} title="Diff against the parent revision">
                            vs parent
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function RunList({ runs }: { runs: Run[] }) {
  const store = useStore();
  const shown = useAppState(effectiveRunId);
  return (
    <Card title={`Runs (${runs.length})`} bodyClassName="p-0">
      {runs.length === 0 ? (
        <div className="p-3">
          <Empty title="No runs yet">Runs appear when a message starts the pipeline.</Empty>
        </div>
      ) : (
        <ul className="divide-y divide-line">
          {runs.map((r) => {
            const st = runStatus(r.status);
            const dur = spanSeconds(r.created_at, r.finished_at);
            return (
              <li key={r.id}>
                <button
                  type="button"
                  onClick={() => store.selectRun(r.id)}
                  className={cx("w-full px-3 py-2 text-left text-xs transition-colors hover:bg-raised", r.id === shown && "bg-accent/5")}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Pill tone={st.tone}>{st.label}</Pill>
                    <span className="font-mono text-muted">{r.trigger}</span>
                    {r.options?.simulation_only && <Pill tone="info">simulation only</Pill>}
                    {r.options?.inject_fault && (
                      <Pill tone="warn" title="This run deliberately injected a defect to demonstrate automatic repair">
                        TEST MODE: {r.options.inject_fault}
                      </Pill>
                    )}
                    <span className="ml-auto whitespace-nowrap text-faint" title={fmtDateTime(r.created_at)}>
                      {fmtAgo(r.created_at)}{r.finished_at ? ` · ${fmtDuration(dur)}` : ""}
                    </span>
                  </div>
                  <div className="mt-1 truncate text-fg" title={r.request_text}>{r.request_text || <span className="text-faint">(no request text)</span>}</div>
                  <div className="mt-0.5 text-[11px] text-faint">
                    {r.revision_ids?.length ? `revisions: ${r.revision_ids.join(", ")}` : "no revision produced"}
                    {r.id === shown && <span className="ml-2 text-accent">shown in pipeline</span>}
                  </div>
                  {r.error && <div className="mt-1 text-[11px] text-bad">{r.error}</div>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

export function HistoryTab() {
  const store = useStore();
  const projectId = useAppState((s) => s.selection.projectId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const projectError = useAppState((s) => (projectId ? s.projectErrors[projectId] : undefined));
  const allRuns = useAppState((s) => s.runs);
  const runs = useMemo(
    () => (detail ? detail.runIds.map((id) => allRuns[id]).filter((r): r is Run => !!r).sort((a, b) => b.created_at - a.created_at) : []),
    [detail, allRuns],
  );

  let right;
  if (!projectId) right = <Empty title="No project selected">Pick a project on the left to see its revisions and runs.</Empty>;
  else if (!detail && projectError) right = <ErrorNote error={projectError} onRetry={() => void store.loadProject(projectId)} />;
  else if (!detail) right = <Loading label="Loading project…" />;
  else
    right = (
      <div className="space-y-3">
        <RevisionTable revisions={detail.revisions} />
        <RunList runs={runs} />
      </div>
    );

  return (
    <div className="grid gap-3 xl:grid-cols-[17rem_minmax(0,1fr)]">
      <ProjectList />
      <div className="min-w-0">{right}</div>
    </div>
  );
}
