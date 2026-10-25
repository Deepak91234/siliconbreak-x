/**
 * SiliconBench: stored benchmark runs over the security cases (GET/POST /api/bench/runs).
 * Every number is computed by the backend from executed records; denominators and
 * not-run counts are always shown, and a provider without a key is NOT RUN, never 0%.
 */

import { useEffect, useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type BenchCaseResult, type BenchEstimate, type BenchRun } from "../api/security";
import { Button, Card, Collapsible, Empty, ErrorNote, Pill, Select, Spinner, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";
import { toast } from "../lib/toast";
import { AICredits, usd } from "./AICredits";

const ARMS: Array<{ id: string; label: string; paid: boolean }> = [
  { id: "harness", label: "Harness validity + formal", paid: false },
  { id: "detect:rules", label: "Detection: offline rules (not AI)", paid: false },
  { id: "detect:bitdeer", label: "Detection: Bitdeer", paid: true },
  { id: "detect:anthropic", label: "Detection: Claude", paid: true },
  { id: "repair:baseline:auto", label: "Repair: model, single shot (no tools)", paid: true },
  { id: "repair:assisted:auto", label: "Repair: same model + verifier loop", paid: true },
  { id: "repair:factory", label: "Repair: Factory Droid", paid: true },
];

const KIND_LABEL: Record<string, string> = {
  demo: "demo", research_injected: "research-injected", historical: "historical", benchmark_adapted: "adapted benchmark",
};

export function SiliconBench() {
  const [runs, setRuns] = useState<BenchRun[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [arms, setArms] = useState<string[]>(["harness", "detect:rules"]);
  const [est, setEst] = useState<BenchEstimate | null>(null);
  useEffect(() => {
    securityApi.benchEstimate({ arms }).then(setEst).catch(() => setEst(null));
  }, [arms]);

  const load = async () => {
    try {
      const list = await securityApi.benchRuns();
      setRuns(list.sort((a, b) => b.created_at - a.created_at));
      setErr(null);
    } catch (e) {
      setErr(errorMessage(e));
    }
  };
  useEffect(() => { void load(); }, []);
  const run = runs?.find((r) => r.id === picked) ?? runs?.[0] ?? null;
  useEffect(() => {
    if (run?.status !== "running") return;
    const id = window.setInterval(() => void load(), 2000);
    return () => window.clearInterval(id);
  }, [run?.id, run?.status]);

  const start = async () => {
    const e = est ?? await securityApi.benchEstimate({ arms });
    if (e.paid) {
      if (!e.live_enabled) {
        toast("Paid arms need live AI calls on (AI credits panel below). Free arms can run now.", "info");
        return;
      }
      const ok = window.confirm(`Run SiliconBench with paid arms?\n\nWorst case: ${usd(e.usd_total)} ` +
        `(Claude ${usd(e.usd.anthropic)}, Bitdeer ${usd(e.usd.bitdeer)}), ${e.droid_sessions} Droid session(s), ` +
        `${e.calls.anthropic + e.calls.bitdeer} model call(s) over ${e.case_ids.length} case(s).\n\n${e.note}.`);
      if (!ok) return;
    }
    setStarting(true);
    try {
      const b = await securityApi.startBench({ arms, confirm_max_usd: e.paid ? e.usd_total : undefined,
                                               confirm_droid_sessions: e.paid ? e.droid_sessions : undefined });
      setPicked(b.id);
      await load();
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <section className="rounded-lg border border-line bg-panel p-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-semibold tracking-tight">SiliconBench</h2>
            <p className="mt-1 text-xs text-muted">
              Runs every security case through the same trusted verifier: harness validity, detection by each investigator, and
              repair by a single-shot model, the same model with the verifier loop, and Factory Droid. Arms without a configured
              provider are reported as not run. Demo and locally authored cases are labelled and are not historical-vulnerability results.
            </p>
          </div>
          {runs && runs.length > 1 && (
            <Select value={run?.id ?? ""} onChange={setPicked} className="max-w-[16rem]">
              {runs.map((r) => <option key={r.id} value={r.id}>{fmtDateTime(r.created_at)} · {r.status}</option>)}
            </Select>
          )}
          <Button variant="primary" size="sm" busy={starting} disabled={run?.status === "running" || arms.length === 0} onClick={start}>
            Run SiliconBench{est?.paid ? ` (≤ ${usd(est.usd_total)}${est.droid_sessions ? `, ${est.droid_sessions} Droid` : ""})` : ""}
          </Button>
        </div>
        <fieldset className="mt-3 grid gap-x-4 gap-y-1 sm:grid-cols-2" aria-label="Benchmark arms">
          {ARMS.map((a) => (
            <label key={a.id} className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={arms.includes(a.id)} className="accent-[rgb(var(--accent))]"
                     onChange={(ev) => setArms((xs) => ev.target.checked ? [...xs, a.id] : xs.filter((x) => x !== a.id))} />
              <span className={a.paid ? "text-fg" : "text-muted"}>{a.label}</span>
              {a.paid && <span className="rounded bg-warn/15 px-1 text-2xs text-warn">paid</span>}
            </label>
          ))}
        </fieldset>
        {est?.paid && (
          <p className="mt-2 text-2xs text-muted">
            Worst case for this selection: {usd(est.usd_total)} (Claude {usd(est.usd.anthropic)}, Bitdeer {usd(est.usd.bitdeer)}),
            {" "}{est.droid_sessions} Droid session(s). The credit guard still refuses any call above its caps.
          </p>
        )}
      </section>
      <AICredits compact />
      {err && <ErrorNote error={err} onRetry={load} />}
      {!runs && !err && <div className="flex items-center gap-2 text-xs text-muted"><Spinner className="h-3.5 w-3.5" /> Loading…</div>}
      {runs && !run && <Empty title="No benchmark runs yet">Press “Run SiliconBench” to execute every case. Results are stored and can be compared later.</Empty>}
      {run && <BenchResult run={run} />}
    </div>
  );
}

function BenchResult({ run }: { run: BenchRun }) {
  const res = run.result;
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Pill tone={run.status === "finished" ? "ok" : run.status === "running" ? "accent" : "bad"}>{run.status}</Pill>
        <span>started {fmtDateTime(run.created_at)}</span>
        {run.finished_at && <span>· {Math.round(run.finished_at - run.created_at)} s</span>}
        <span>· {run.config.case_ids.length} case(s)</span>
        {run.status === "running" && <Spinner className="h-3.5 w-3.5" />}
      </div>
      {res && (
        <Card title="Metrics (computed from stored records)">
          <ul className="grid gap-2 sm:grid-cols-2">
            {Object.entries(res.metrics).map(([k, m]) => {
              const ran = m.denominator > 0;
              return (
                <li key={k} className="rounded-md border border-line p-2.5">
                  <div className="text-xs text-muted">{m.label}</div>
                  <div className="mt-1 flex items-baseline gap-2">
                    {ran ? (
                      <span className={cx("text-xl font-semibold tabular-nums", m.numerator === m.denominator ? "text-ok" : "text-fg")}>
                        {m.numerator}/{m.denominator}
                      </span>
                    ) : <span className="text-sm font-medium text-orange">not run</span>}
                    {m.not_run > 0 && <span className="text-2xs text-faint">{m.not_run} not run</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        </Card>
      )}
      {res?.cases.map((c) => <CaseRow key={c.case_id} c={c} />)}
      {res && Object.keys(res.tool_versions ?? {}).length > 0 && (
        <p className="text-2xs text-faint">{Object.entries(res.tool_versions).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>
      )}
    </>
  );
}

function verdictPill(status: string, ok: boolean | null, okLabel: string, badLabel: string, reason?: string) {
  if (status === "not_run") return <Pill tone="orange" title={reason}>not run</Pill>;
  if (status === "error") return <Pill tone="bad" title={reason}>error</Pill>;
  if (ok === null) return <Pill tone="idle">n/a</Pill>;
  return <Pill tone={ok ? "ok" : "bad"}>{ok ? okLabel : badLabel}</Pill>;
}

function CaseRow({ c }: { c: BenchCaseResult }) {
  const h = c.harness;
  return (
    <Card title={c.title} actions={<span className="flex items-center gap-1.5"><Pill tone="violet">{KIND_LABEL[c.source_kind] ?? c.source_kind}</Pill>
      <span className="text-2xs text-faint">{c.cwe.join(", ")}</span></span>}>
      <p className="mb-2 text-2xs text-faint">{c.provenance}{c.repository ? ` · ${c.repository}@${c.commit}` : ""}</p>
      {h && (
        <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <span>vulnerable: {verdictPill(h.vulnerable_status === "error" ? "error" : "ran", h.vulnerable_fails, "fails security tests", "passes (no defect)")}</span>
          <span>reference: {verdictPill(h.reference_status === "not_run" ? "not_run" : "ran", h.reference_passes, "passes all tests", "fails")}</span>
          <span>formal: <span className="font-mono text-2xs">{h.formal_vulnerable ?? "–"} → {h.formal_reference ?? "–"}</span></span>
          {h.failed_properties.length > 0 && <span className="text-muted">violated {h.failed_properties.join(", ")}</span>}
        </div>
      )}
      <table className="w-full text-xs">
        <tbody>
          {Object.entries(c.detection).map(([arm, d]) => (
            <tr key={arm} className="border-t border-line">
              <td className="py-1.5 pr-2 text-muted">{arm}</td>
              <td className="py-1.5">{verdictPill(d.status, d.correct, "found the defect", "missed", d.reason)}</td>
              <td className="py-1.5 text-2xs text-faint">{d.status === "ran" ? `${d.findings} finding(s) · ${d.model ?? ""}${d.is_ai ? "" : " (not AI)"}` : d.reason}</td>
            </tr>
          ))}
          {Object.entries(c.repair).map(([arm, r]) => (
            <tr key={arm} className="border-t border-line">
              <td className="py-1.5 pr-2 text-muted">{arm}</td>
              <td className="py-1.5">{verdictPill(r.status, r.verdict ? r.verdict === "verified" : null, "verified", r.verdict ?? "failed", r.reason)}</td>
              <td className="py-1.5 text-2xs text-faint">{r.status === "ran" ? `${r.attempts} attempt(s) · ${r.verdict}` : r.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Collapsible title={<span className="text-2xs text-faint">Reproduce this case</span>}>
        <pre className="whitespace-pre-wrap rounded bg-sunken p-2 font-mono text-2xs text-muted">
{`curl -s -X POST http://127.0.0.1:8000/api/bench/runs -H 'content-type: application/json' \\
  -d '{"case_ids": ["${c.case_id}"]}'`}
        </pre>
      </Collapsible>
      <p className="mt-1 text-2xs text-faint">{c.duration_s.toFixed(1)} s</p>
    </Card>
  );
}
