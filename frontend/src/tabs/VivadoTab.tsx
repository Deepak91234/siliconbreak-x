/**
 * Vivado build of a revision: GET /api/revisions/{id}/build (VivadoBuildResult),
 * the live log from `log` events of the build stages (falling back to the
 * stored vivado.log tail), timing, utilization, DRC, acceptance checks,
 * bitstream identity, raw reports, and the Rebuild action.
 */

import { useEffect, useMemo, useState } from "react";
import { api, errorMessage, fileUrl, runsRelative } from "../api/client";
import type { Run, UtilResource, Utilization, VivadoBuildResult } from "../api/types";
import { REPORT_NAMES } from "../api/types";
import { isRunActive } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { fmtBytes, fmtDuration, fmtNs, fmtPct, humanize, shortSha } from "../lib/format";
import { boolTone, buildStatus, stageStatus, type Tone } from "../lib/status";
import { toast } from "../lib/toast";
import { useResource } from "../lib/useResource";
import { Bar, Button, Card, CopyButton, Empty, ErrorNote, KV, Loading, Metric, Pill, Pre, Select, cx } from "../components/ui";
import { StatusIcon } from "../components/StatusIcon";
import { useRevision } from "./VerilogTab";
import { LogView } from "./LogView";

const BUILD_STAGES = ["synth", "impl", "timing", "bitstream"];

const UTIL_ROWS: Array<{ key: string; label: string }> = [
  { key: "lut", label: "LUT" },
  { key: "ff", label: "FF" },
  { key: "iob", label: "IOB" },
  { key: "bufg", label: "BUFG" },
  { key: "bram", label: "BRAM" },
  { key: "dsp", label: "DSP" },
];

function slackTone(ns: number | null | undefined): Tone {
  if (ns === null || ns === undefined) return "idle";
  return ns >= 0 ? "ok" : "bad";
}

function utilFor(u: Utilization | null, key: string): { used: number; available: number; percent: number } | null {
  if (!u) return null;
  if (u.summary?.[key]) return u.summary[key];
  const r: UtilResource | undefined = u.resources?.find((x) => x.key === key);
  return r ? { used: r.used, available: r.available, percent: r.percent } : null;
}

function UtilizationCard({ util }: { util: Utilization | null }) {
  if (!util) return <Card title="Utilization (implemented)"><p className="text-xs text-muted">No utilization report.</p></Card>;
  return (
    <Card title="Utilization (implemented)">
      <div className="space-y-2">
        {UTIL_ROWS.map(({ key, label }) => {
          const u = utilFor(util, key);
          return (
            <div key={key} className="grid grid-cols-[3.5rem_1fr_7.5rem] items-center gap-2 text-xs">
              <span className="font-medium">{label}</span>
              {u ? <Bar value={u.percent} max={100} tone={u.percent > 90 ? "bad" : u.percent > 70 ? "warn" : "accent"} /> : <span className="text-[11px] text-faint">not reported</span>}
              <span className="text-right font-mono text-[11px] tabular-nums text-muted">
                {u ? `${u.used.toLocaleString()} / ${u.available.toLocaleString()} (${fmtPct(u.percent)})` : ""}
              </span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function ReportViewer({ revisionId, version }: { revisionId: string; version: number }) {
  const [name, setName] = useState<string>("timing_summary.rpt");
  const res = useResource(`${revisionId}:${name}`, version, (signal) => api.report(revisionId, name, signal));
  return (
    <Card title="Vivado reports" actions={
      <Select value={name} onChange={setName}>
        {REPORT_NAMES.map((n) => <option key={n} value={n}>{n}</option>)}
      </Select>
    }>
      {res.loading && !res.data && <Loading />}
      {res.notFound && <p className="text-xs text-muted">{name} was not produced for this build.</p>}
      {res.error && !res.notFound && <ErrorNote error={res.error} onRetry={res.reload} />}
      {res.data && <Pre maxH="max-h-[28rem]">{res.data.content}</Pre>}
    </Card>
  );
}

function BuildDetails({ b, runsDir }: { b: VivadoBuildResult; runsDir: string | null }) {
  const t = b.timing;
  const bitRel = runsRelative(b.bitstream_path, runsDir);
  const st = buildStatus(b.ok ? "passed" : b.status);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Pill tone={st.tone} className="text-xs">{b.ok ? "build passed" : `build ${b.status}`}</Pill>
        {b.failed_stage && <Pill tone="bad">failed in {b.failed_stage}</Pill>}
        <span className="text-muted">{b.message}</span>
        <span className="ml-auto text-[11px] text-faint">
          {b.vivado_version ?? "Vivado ?"} · {b.container ?? "container ?"} · {fmtDuration(b.duration_s)}
          {b.exit_code != null ? ` · exit ${b.exit_code}` : ""}
        </span>
      </div>

      {Object.keys(b.stage_times ?? {}).length > 0 && (
        <div className="flex flex-wrap gap-2 text-[11px] text-muted">
          {Object.entries(b.stage_times).map(([k, v]) => (
            <span key={k} className="rounded bg-sunken px-2 py-0.5"><span className="text-fg">{k}</span> {fmtDuration(v)}</span>
          ))}
        </div>
      )}

      <Card title={<span className="flex items-center gap-2">Timing {t && <Pill tone={t.timing_met ? "ok" : "bad"}>{t.timing_met ? "met" : "not met"}</Pill>}</span>}>
        {t ? (
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <Metric label="WNS (setup)" value={fmtNs(t.wns_ns)} tone={slackTone(t.wns_ns)} />
              <Metric label="TNS" value={fmtNs(t.tns_ns)} tone={t.tns_ns === null ? "idle" : t.tns_ns < 0 ? "bad" : "ok"} sub={t.tns_failing_endpoints != null ? `${t.tns_failing_endpoints} failing endpoints` : undefined} />
              <Metric label="WHS (hold)" value={fmtNs(t.whs_ns)} tone={slackTone(t.whs_ns)} />
              <Metric label="THS" value={fmtNs(t.ths_ns)} tone={t.ths_ns === null ? "idle" : t.ths_ns < 0 ? "bad" : "ok"} sub={t.ths_failing_endpoints != null ? `${t.ths_failing_endpoints} failing endpoints` : undefined} />
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
              <span>WPWS {fmtNs(t.wpws_ns)}</span>
              <span>{t.total_endpoints ?? "?"} endpoints</span>
              <span>constraints met: {t.constraints_met === null ? "?" : t.constraints_met ? "yes" : "no"}</span>
              {t.clocks?.map((c) => (
                <span key={c.name}>clock <span className="font-mono text-fg">{c.name}</span> {c.period_ns ?? "?"} ns ({c.freq_mhz ?? "?"} MHz)</span>
              ))}
            </div>
            {t.unconstrained_note && <p className="text-[11px] text-warn">{t.unconstrained_note}</p>}
          </div>
        ) : (
          <p className="text-xs text-muted">No timing summary in this build result.</p>
        )}
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <UtilizationCard util={b.utilization} />
        <Card title="DRC">
          {b.drc ? (
            <div className="space-y-2">
              <div className="grid grid-cols-3 gap-2">
                <Metric label="Errors" value={b.drc.errors} tone={b.drc.errors ? "bad" : "ok"} />
                <Metric label="Critical warn." value={b.drc.critical_warnings} tone={b.drc.critical_warnings ? "orange" : "ok"} />
                <Metric label="Warnings" value={b.drc.warnings} tone={b.drc.warnings ? "warn" : "ok"} />
              </div>
              {b.drc.violations?.length > 0 && (
                <ul className="max-h-40 space-y-1 overflow-auto text-[11px]">
                  {b.drc.violations.map((v, i) => (
                    <li key={i} className="rounded border border-line px-2 py-1">
                      <span className="font-mono">{v.rule}</span> <span className="text-muted">{v.severity} ×{v.count}</span> – {v.description}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted">No DRC report.</p>
          )}
        </Card>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card title="Acceptance checks">
          {b.checks?.length ? (
            <ul className="space-y-1">
              {b.checks.map((c) => (
                <li key={c.name} className="flex items-start gap-2 text-xs">
                  <StatusIcon status={c.passed ? "passed" : "failed"} className="mt-0.5 h-3.5 w-3.5" />
                  <span className="font-medium">{humanize(c.name)}</span>
                  {c.detail && <span className="text-muted">{c.detail}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted">No acceptance checks reported.</p>
          )}
        </Card>
        <Card title="Bitstream">
          {b.bitstream_sha256 || b.bitstream ? (
            <KV items={[
              ["Valid", <Pill key="v" tone={boolTone(b.bitstream?.valid)}>{b.bitstream ? (b.bitstream.valid ? "valid" : "invalid") : "not checked"}</Pill>],
              ["SHA-256", b.bitstream_sha256 ? <span className="flex flex-wrap items-center gap-2 font-mono" title={b.bitstream_sha256}>{shortSha(b.bitstream_sha256, 24)} <CopyButton text={b.bitstream_sha256} /></span> : "–"],
              ["Size", fmtBytes(b.bitstream?.size_bytes)],
              ["Part", b.bitstream?.part ?? "–"],
              ["Design", b.bitstream?.design_field ?? "–"],
              ["Written", [b.bitstream?.date, b.bitstream?.time].filter(Boolean).join(" ") || "–"],
              ["Sync word", b.bitstream ? (b.bitstream.sync_word_found ? "found" : "missing") : "–"],
              ["Problem", b.bitstream?.problem ?? "none"],
              ["File", bitRel ? <a className="font-mono text-accent hover:underline" href={fileUrl(bitRel)} download>{b.bitstream_path?.split("/").pop()}</a> : <span className="font-mono">{b.bitstream_path ?? "–"}</span>],
            ]} />
          ) : (
            <p className="text-xs text-muted">No bitstream was produced.</p>
          )}
        </Card>
      </div>

      {b.log_summary && (b.log_summary.errors.length > 0 || b.log_summary.critical_warnings.length > 0) && (
        <Card title={`Log summary (${b.log_summary.warning_count} warnings)`}>
          <ul className="space-y-1 font-mono text-[11px]">
            {b.log_summary.errors.map((e, i) => <li key={`e${i}`} className="text-bad">{e}</li>)}
            {b.log_summary.critical_warnings.map((e, i) => <li key={`c${i}`} className="text-orange">{e}</li>)}
          </ul>
        </Card>
      )}
    </div>
  );
}

export function VivadoTab({ revisionId, run }: { revisionId: string | null; run: Run | null }) {
  const store = useStore();
  const env = useAppState((s) => s.environment);
  const rev = useRevision(revisionId);
  const build = useResource(revisionId, rev.version, (signal) => api.build(revisionId!, signal));
  const logs = useAppState((s) => (run ? s.logs[run.id] : undefined));
  const buildStages = run?.stages.filter((s) => BUILD_STAGES.includes(s.key)) ?? [];
  const building = buildStages.some((s) => s.status === "running");
  const liveLines = useMemo(
    () => (logs ?? []).filter((l) => l.source === "vivado" || (l.stage && BUILD_STAGES.includes(l.stage))).map((l) => l.line),
    [logs],
  );
  // Stored log tail (after the build, or after a reload when no live lines are in memory).
  const logRes = useResource(revisionId, `${rev.version}:${building ? "live" : "done"}`, (signal) => api.buildLog(revisionId!, 2000, signal));
  const [rebuilding, setRebuilding] = useState(false);

  useEffect(() => {
    if (run && building) void store.backfillRunLogs(run.id);
  }, [run, building, store]);

  if (!revisionId) return <Empty title="No Vivado build yet">Builds run after a revision passes simulation.</Empty>;

  const viv = env?.vivado;
  const canRebuild = !!rev.data && !isRunActive(run) && (rev.data.sim_status === "passed" || rev.data.build_status !== null);

  const rebuild = async () => {
    setRebuilding(true);
    try {
      await store.rebuild(revisionId);
      toast("Rebuild started (Vivado on the same RTL)", "ok");
    } catch (e) {
      toast(`Rebuild refused: ${errorMessage(e)}`, "error");
    } finally {
      setRebuilding(false);
    }
  };

  const useLive = liveLines.length > 0 && (building || !logRes.data);
  const logLines = useLive ? liveLines : logRes.data?.lines ?? [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-panel px-3 py-2 text-xs">
        {buildStages.length > 0 ? (
          buildStages.map((s) => (
            <span key={s.key} className="flex items-center gap-1.5" title={s.summary ?? undefined}>
              <StatusIcon status={s.status} className="h-3.5 w-3.5" />
              {s.label}
              <span className="text-faint">{stageStatus(s.status).label}</span>
            </span>
          ))
        ) : (
          <span className="text-muted">No run selected for this revision.</span>
        )}
        <span className="ml-auto flex items-center gap-2">
          {viv && (
            <span className={cx("text-[11px]", viv.ready ? "text-muted" : "text-warn")}>
              {viv.ready ? `Vivado ${viv.vivado_version ?? ""} in ${viv.active_container}` : "Vivado not ready"}
              {viv.build_running ? " · a build is running" : ""}
            </span>
          )}
          <Button size="sm" variant="secondary" busy={rebuilding} disabled={!canRebuild} onClick={() => void rebuild()} title={canRebuild ? "Run Vivado again on this revision's RTL" : "Available for a simulated revision when no run is active"}>
            Rebuild
          </Button>
        </span>
      </div>

      {build.loading && !build.data && <Loading label="Loading build result…" />}
      {!build.data && !build.loading && (
        build.notFound || !build.error ? (
          <Empty title={building ? "Vivado build running…" : "No Vivado build for this revision"}>
            {building
              ? "Results appear when the build finishes; the log below streams live."
              : viv && !viv.ready
                ? `Vivado is not available: ${viv.problems?.join("; ") || "container not running"}. Start it from the status bar (requires approval), then use Rebuild.`
                : "This revision has not been built (simulation may have failed, or the run was simulation-only)."}
          </Empty>
        ) : (
          <ErrorNote error={build.error} onRetry={build.reload} />
        )
      )}
      {build.data && <BuildDetails b={build.data} runsDir={(env?.config?.runs_dir as string | undefined) ?? null} />}

      <Card title={`Vivado log${useLive ? (building ? " (live)" : " (this session)") : logRes.data ? ` (last ${logRes.data.lines.length} of ${logRes.data.total} lines)` : ""}`}>
        {logLines.length === 0 && logRes.loading ? (
          <Loading />
        ) : (
          <LogView lines={logLines} follow={building || useLive} height="h-96" emptyText={logRes.notFound ? "No vivado.log for this revision." : "No log lines yet."} />
        )}
      </Card>

      {build.data && <ReportViewer revisionId={revisionId} version={rev.version} />}
    </div>
  );
}
