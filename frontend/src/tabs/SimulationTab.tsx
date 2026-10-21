/**
 * Simulation evidence for a revision: GET /api/revisions/{id}/simulation
 * (SimulationReport, contracts §A) - lint at real and simulation parameters,
 * scenarios and their checks, failure diagnostics, the requirement table with
 * simulation and physical status, and the waveform viewer. While the run is
 * still linting/simulating, the live Verilator log comes from `log` events.
 */

import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { CheckResult, LintBlock, Revision, Run, ScenarioResult, SimulationReport } from "../api/types";
import { useAppState, useStore } from "../store/store";
import { compactJson, fmtDuration, prettyJson } from "../lib/format";
import { physicalStatus, simReportStatus, simRequirementStatus, stageStatus } from "../lib/status";
import { useResource } from "../lib/useResource";
import { Card, Collapsible, Empty, ErrorNote, Loading, Metric, Pill, Pre, cx } from "../components/ui";
import { StatusIcon } from "../components/StatusIcon";
import { useRevision } from "./VerilogTab";
import { WaveformViewer } from "./WaveformViewer";
import { LogView } from "./LogView";

function lintBlocks(lint: SimulationReport["lint"]): Array<[string, LintBlock]> {
  if (!lint || typeof lint !== "object") return [];
  const l = lint as Record<string, unknown>;
  if (typeof l.ok === "boolean" && Array.isArray(l.messages)) return [[String(l.params ?? "lint"), l as unknown as LintBlock]];
  return Object.entries(l)
    .filter(([, v]) => v && typeof v === "object" && Array.isArray((v as LintBlock).messages))
    .map(([k, v]) => [k, v as LintBlock]);
}

function LintSection({ report }: { report: SimulationReport }) {
  const blocks = lintBlocks(report.lint);
  if (!blocks.length) return <p className="text-xs text-muted">No lint results in the report.</p>;
  return (
    <div className="space-y-2">
      {blocks.map(([name, b]) => (
        <div key={name}>
          <div className="mb-1 flex items-center gap-2 text-xs">
            <StatusIcon status={b.ok ? "passed" : "failed"} className="h-3.5 w-3.5" />
            <span className="font-medium">Verilator lint – {name} parameters</span>
            <span className="text-muted">{b.messages.length} messages</span>
          </div>
          {b.messages.length > 0 && (
            <ul className="space-y-1">
              {b.messages.map((m, i) => (
                <li key={i} className={cx("rounded border px-2 py-1 font-mono text-[11px]", m.severity?.toLowerCase().includes("error") || m.fatal ? "border-bad/30 text-bad" : "border-warn/30 text-warn")}>
                  <span className="font-semibold">%{m.severity}{m.code ? `-${m.code}` : ""}</span>{" "}
                  {m.file ? `${m.file}:${m.line ?? "?"}${m.column ? `:${m.column}` : ""}: ` : ""}
                  <span className="text-fg/90">{m.message}</span>
                  {m.context?.length > 0 && <pre className="mt-1 whitespace-pre-wrap text-faint">{m.context.join("\n")}</pre>}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}

function CheckRow({ c }: { c: CheckResult }) {
  const [open, setOpen] = useState(!c.passed);
  const hasMore = c.expected !== undefined || c.observed !== undefined || (c.details && Object.keys(c.details).length > 0);
  return (
    <li className={cx("rounded-md border px-2 py-1.5", c.passed ? "border-line" : "border-bad/40 bg-bad/5")}>
      <button type="button" className="flex w-full items-start gap-2 text-left" onClick={() => setOpen(!open)}>
        <StatusIcon status={c.passed ? "passed" : "failed"} className="mt-0.5 h-3.5 w-3.5" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-mono text-[11px] text-muted">{c.id}</span>
            <span className="text-xs">{c.summary}</span>
          </div>
          <div className="flex flex-wrap gap-1 pt-0.5">
            {c.requirement_ids.map((r) => <Pill key={r} tone="muted">{r}</Pill>)}
            {!c.passed && c.first_failure_cycle != null && <Pill tone="bad">first failure @ cycle {c.first_failure_cycle}</Pill>}
          </div>
        </div>
      </button>
      {open && hasMore && (
        <div className="mt-1.5 grid gap-2 pl-5 md:grid-cols-2">
          {c.expected !== undefined && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-faint">expected</div>
              <Pre maxH="max-h-40">{prettyJson(c.expected)}</Pre>
            </div>
          )}
          {c.observed !== undefined && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-faint">observed</div>
              <Pre maxH="max-h-40">{prettyJson(c.observed)}</Pre>
            </div>
          )}
          {c.details && Object.keys(c.details).length > 0 && (
            <div className="md:col-span-2">
              <div className="text-[10px] uppercase tracking-wide text-faint">details</div>
              <Pre maxH="max-h-48">{prettyJson(c.details)}</Pre>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function ScenarioCard({ s, report, onWave }: { s: ScenarioResult; report: SimulationReport; onWave: (id: string) => void }) {
  const plan = report.plan?.scenarios.find((p) => p.id === s.id);
  const failed = s.checks.filter((c) => !c.passed).length;
  return (
    <Collapsible
      defaultOpen={!s.passed}
      title={
        <span className="flex items-center gap-2">
          <StatusIcon status={s.passed ? "passed" : "failed"} className="h-3.5 w-3.5" />
          <span className="font-mono text-muted">{s.id}</span>
          <span>{s.title}</span>
        </span>
      }
      right={
        <span className="flex items-center gap-2 text-[11px] text-muted">
          {failed ? <span className="text-bad">{failed} failing</span> : null}
          <span>{s.checks.length} checks</span>
          {s.duration_s != null && <span>{fmtDuration(s.duration_s)}</span>}
        </span>
      }
    >
      <div className="space-y-2">
        {plan && (
          <div className="text-xs text-muted">
            {plan.description}
            <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-faint">
              <span>{plan.events.length} stimulus events</span>
              <span>ends at cycle {plan.end_cycle}</span>
              {plan.vcd_window && <span>VCD window {plan.vcd_window[0]}&ndash;{plan.vcd_window[1]}</span>}
            </div>
            {plan.events.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {plan.events.slice(0, 24).map((e, i) => (
                  <span key={i} className="rounded bg-sunken px-1.5 py-0.5 font-mono text-[10px]">@{e.cycle} {e.port}={e.value}</span>
                ))}
                {plan.events.length > 24 && <span className="text-[10px] text-faint">+{plan.events.length - 24} more</span>}
              </div>
            )}
          </div>
        )}
        <ul className="space-y-1">
          {s.checks.map((c) => <CheckRow key={c.id} c={c} />)}
        </ul>
        {s.vcd_file && (
          <button type="button" className="text-[11px] text-accent hover:underline" onClick={() => onWave(s.id)}>
            Show waveform for {s.id}
          </button>
        )}
      </div>
    </Collapsible>
  );
}

function RequirementsTable({ report, revision }: { report: SimulationReport; revision: Revision | null }) {
  const physical = new Map((revision?.verification?.requirements ?? []).map((r) => [r.id, r.physical_status]));
  const methods = new Map((revision?.requirements ?? []).map((r) => [r.id, r]));
  const rows = report.requirements?.length
    ? report.requirements
    : (revision?.requirements ?? []).map((r) => ({ id: r.id, text: r.text, methods: r.methods, sim_status: "not_tested" as const }));
  if (!rows.length) return <p className="text-xs text-muted">No requirements in the report.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-[10px] uppercase tracking-wide text-faint">
          <tr>
            <th className="py-1 pr-3 font-medium">ID</th>
            <th className="py-1 pr-3 font-medium">Requirement</th>
            <th className="py-1 pr-3 font-medium">Methods</th>
            <th className="py-1 pr-3 font-medium">Simulation</th>
            <th className="py-1 font-medium">Physical</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((r) => {
            const sim = simRequirementStatus(r.sim_status);
            const phys = physical.get(r.id);
            const ph = phys ? physicalStatus(phys) : null;
            const note = methods.get(r.id)?.physical_note;
            return (
              <tr key={r.id} className="align-top">
                <td className="py-1.5 pr-3 font-mono text-[11px] text-muted">{r.id}</td>
                <td className="py-1.5 pr-3">{r.text}</td>
                <td className="py-1.5 pr-3 text-[11px] text-muted">{(r.methods ?? []).join(", ")}</td>
                <td className="py-1.5 pr-3"><Pill tone={sim.tone}>{sim.label}</Pill></td>
                <td className="py-1.5" title={note ?? undefined}>
                  {ph ? <Pill tone={ph.tone}>{ph.label}</Pill> : <span className="text-[11px] text-faint">{(r.methods ?? []).includes("physical") ? "not verified yet" : "simulation only"}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Diagnostics({ report }: { report: SimulationReport }) {
  const d = report.diagnostics ?? {};
  if (!Object.keys(d).length) return null;
  return (
    <Card title="Failure diagnostics (sent to the repair step)">
      <Pre maxH="max-h-96">{prettyJson(d)}</Pre>
    </Card>
  );
}

const SIM_STAGES = ["lint", "tests", "simulate", "repair"];

export function SimulationTab({ revisionId, run }: { revisionId: string | null; run: Run | null }) {
  const store = useStore();
  const rev = useRevision(revisionId);
  const report = useResource(revisionId, rev.version, (signal) => api.simulation(revisionId!, signal));
  const logs = useAppState((s) => (run ? s.logs[run.id] : undefined));
  const [waveScenario, setWaveScenario] = useState<string | null>(null);
  const simStages = run?.stages.filter((s) => SIM_STAGES.includes(s.key)) ?? [];
  const simActive = simStages.some((s) => s.status === "running");
  const verilatorLines = useMemo(
    () => (logs ?? []).filter((l) => l.source === "verilator" || (l.stage && SIM_STAGES.includes(l.stage))),
    [logs],
  );
  useEffect(() => {
    if (run && simActive) void store.backfillRunLogs(run.id);
  }, [run?.id, simActive, store, run]);

  if (!revisionId) return <Empty title="No simulation yet">Simulation results appear once a revision has been generated and simulated.</Empty>;

  const stageStrip = simStages.length > 0 && (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-panel px-3 py-2 text-xs">
      {simStages.map((s) => (
        <span key={s.key} className="flex items-center gap-1.5" title={s.summary ?? undefined}>
          <StatusIcon status={s.status} className="h-3.5 w-3.5" />
          {s.label}
          <span className="text-faint">{stageStatus(s.status).label}</span>
          {s.summary ? <span className="max-w-[16rem] truncate text-muted">{s.summary}</span> : null}
        </span>
      ))}
    </div>
  );

  const liveLog = verilatorLines.length > 0 && (
    <Card title={`Simulation log${simActive ? " (live)" : ""}`}>
      <LogView lines={verilatorLines.map((l) => l.line)} follow={simActive} height="h-56" />
    </Card>
  );

  if (report.loading && !report.data) return <div className="space-y-3">{stageStrip}<Loading label="Loading simulation report…" /></div>;
  if (!report.data) {
    return (
      <div className="space-y-3">
        {stageStrip}
        {report.notFound || !report.error ? (
          <Empty title={simActive ? "Simulation running…" : "No simulation yet"}>
            {simActive
              ? "The report will appear here when the simulation suite finishes; live progress is shown above."
              : "This revision has no simulation report. It may not have reached the simulation stage, or Verilator was unavailable."}
          </Empty>
        ) : (
          <ErrorNote error={report.error} onRetry={report.reload} />
        )}
        {liveLog}
      </div>
    );
  }

  const r = report.data;
  const st = simReportStatus(r.status);
  const failedScenarios = r.scenarios.filter((s) => !s.passed).length;
  const build = r.build as { ok?: boolean; duration_s?: number; log_tail?: string };

  return (
    <div className="space-y-3">
      {stageStrip}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Metric label="Result" value={<Pill tone={st.tone} className="text-xs">{st.label}</Pill>} />
        <Metric label="Checks passed" value={r.passed_checks} tone="ok" />
        <Metric label="Checks failed" value={r.failed_checks} tone={r.failed_checks ? "bad" : "idle"} />
        <Metric label="Scenarios" value={`${r.scenarios.length - failedScenarios}/${r.scenarios.length}`} sub="passing" tone={failedScenarios ? "bad" : "ok"} />
        <Metric label="Duration" value={fmtDuration(r.duration_s)} sub={r.plan ? `sim clock ${r.plan.clk_hz.toLocaleString()} Hz` : undefined} />
      </div>

      <div className="grid gap-3 2xl:grid-cols-2">
        <Card title="Lint">
          <LintSection report={r} />
        </Card>
        <Card title="Harness build">
          <div className="flex items-center gap-2 text-xs">
            <StatusIcon status={build.ok === true ? "passed" : build.ok === false ? "failed" : "pending"} className="h-3.5 w-3.5" />
            <span>{build.ok === true ? "Verilator harness built" : build.ok === false ? "Harness build failed" : "Not reported"}</span>
            {build.duration_s != null && <span className="text-muted">{fmtDuration(build.duration_s)}</span>}
          </div>
          {build.log_tail && <Pre className="mt-2" maxH="max-h-40">{build.log_tail}</Pre>}
          {r.plan && (
            <div className="mt-2 text-[11px] text-muted">
              Parameters: <span className="font-mono">{compactJson(r.plan.params, 400)}</span>
            </div>
          )}
        </Card>
      </div>

      <Card title={`Scenarios (${r.scenarios.length})`} bodyClassName="space-y-1.5 p-3">
        {r.scenarios.length === 0 ? <p className="text-xs text-muted">The report contains no scenarios.</p> : r.scenarios.map((s) => <ScenarioCard key={s.id} s={s} report={r} onWave={setWaveScenario} />)}
      </Card>

      <Diagnostics report={r} />

      <Card title="Requirements">
        <RequirementsTable report={r} revision={rev.data} />
      </Card>

      <Card title="Waveforms">
        <WaveformViewer revisionId={revisionId} report={r} version={rev.version} initialScenario={waveScenario} />
      </Card>

      {liveLog}
    </div>
  );
}
