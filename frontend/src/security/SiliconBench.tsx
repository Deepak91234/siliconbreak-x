/**
 * SiliconBench: stored benchmark runs over the security cases (GET/POST /api/bench/runs).
 * Every number is computed by the backend from executed records; denominators and
 * not-run counts are always shown, and a provider without a key is NOT RUN, never 0%.
 */

import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type BenchCaseResult, type BenchEstimate, type BenchRun } from "../api/security";
import { IconBars } from "../components/icons";
import { Button, Collapsible, Empty, ErrorNote, Pill, Select, Spinner, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";
import { toast } from "../lib/toast";
import { usd } from "./AICredits";

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

  const paidSel = ARMS.filter((a) => a.paid && arms.includes(a.id)).length;
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <section className="rounded-2xl border border-line bg-panel p-5 shadow-panel">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-xl font-semibold tracking-tight">Benchmark security agents on one trusted verifier</h2>
            <p className="mt-1 text-xs text-muted">Arms without a configured provider show <span className="font-medium text-orange">not run</span>, never 0%. Demo cases are labelled and are not historical results.</p>
          </div>
          {runs && runs.length > 1 && (
            <Select value={run?.id ?? ""} onChange={setPicked} className="max-w-[16rem]" title="Earlier runs">
              {runs.map((r) => <option key={r.id} value={r.id}>{fmtDateTime(r.created_at)} · {r.status}</option>)}
            </Select>
          )}
          <Button variant="primary" busy={starting} disabled={run?.status === "running" || arms.length === 0} onClick={start}>
            Run benchmark{est?.paid ? ` (≤ ${usd(est.usd_total)}${est.droid_sessions ? `, ${est.droid_sessions} Droid` : ""})` : " (free)"}
          </Button>
        </div>
        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          {([["Free arms", false], ["Paid arms", true]] as const).map(([title, paid]) => (
            <fieldset key={title} className="rounded-xl border border-line bg-sunken/40 p-3" aria-label={title}>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-semibold">{title}</span>
                {paid && <span className="text-[11px] text-faint">{paidSel} selected · confirms its worst case before running</span>}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {ARMS.filter((a) => a.paid === paid).map((a) => {
                  const on = arms.includes(a.id);
                  return (
                    <label key={a.id} className={cx("flex cursor-pointer items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs ring-1 ring-inset transition-colors",
                      on ? "bg-accent/10 text-fg ring-accent/40" : "bg-panel text-muted ring-line hover:text-fg")}>
                      <input type="checkbox" checked={on} className="accent-[rgb(var(--accent))]"
                             onChange={(ev) => setArms((xs) => ev.target.checked ? [...xs, a.id] : xs.filter((x) => x !== a.id))} />
                      {a.label}
                    </label>
                  );
                })}
              </div>
            </fieldset>
          ))}
        </div>
        {est?.paid && (
          <Collapsible title={<span className="text-xs">Cost: worst case <span className="font-semibold text-fg">{usd(est.usd_total)}</span>, {est.droid_sessions} Droid session(s)</span>}>
            <p className="text-xs text-muted">
              Claude {usd(est.usd.anthropic)} over {est.calls.anthropic} call(s), Bitdeer {usd(est.usd.bitdeer)} over {est.calls.bitdeer} call(s), {est.case_ids.length} case(s).
              {" "}{est.note}. The credit guard still refuses any call above its caps (Settings & usage).
            </p>
          </Collapsible>
        )}
      </section>
      {err && <ErrorNote error={err} onRetry={load} />}
      {!runs && !err && <div className="flex items-center gap-2 text-xs text-muted"><Spinner className="h-3.5 w-3.5" /> Loading…</div>}
      {runs && !run && <Empty title="No benchmark runs yet" icon={<IconBars size={20} />}>Press Run benchmark. The free arms (harness and offline rules) cost nothing; results are stored for comparison.</Empty>}
      {run && <BenchResult run={run} />}
    </div>
  );
}

function Ring({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? value / max : 0;
  const r = 20, c = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 48 48" className="h-11 w-11 -rotate-90" aria-hidden>
      <circle cx="24" cy="24" r={r} fill="none" stroke="rgb(var(--line))" strokeWidth="5" />
      <circle cx="24" cy="24" r={r} fill="none" stroke={pct === 1 ? "rgb(var(--ok))" : "rgb(var(--accent))"} strokeWidth="5" strokeLinecap="round"
              strokeDasharray={`${c * pct} ${c}`} />
    </svg>
  );
}

function BenchResult({ run }: { run: BenchRun }) {
  const res = run.result;
  // Per-arm comparison computed from the stored per-case records.
  const armRows = useMemo(() => {
    if (!res) return [] as Array<{ arm: string; ok: number; ran: number; notRun: number }>;
    const m = new Map<string, { arm: string; ok: number; ran: number; notRun: number }>();
    for (const c of res.cases) {
      for (const [arm, d] of Object.entries(c.detection)) {
        const e = m.get(arm) ?? { arm, ok: 0, ran: 0, notRun: 0 };
        if (d.status === "ran") { e.ran++; if (d.correct) e.ok++; } else e.notRun++;
        m.set(arm, e);
      }
      for (const [arm, r] of Object.entries(c.repair)) {
        const e = m.get(arm) ?? { arm, ok: 0, ran: 0, notRun: 0 };
        if (r.status === "ran") { e.ran++; if (r.verdict === "verified") e.ok++; } else e.notRun++;
        m.set(arm, e);
      }
    }
    return [...m.values()];
  }, [res]);
  const detectArms = res ? [...new Set(res.cases.flatMap((c) => Object.keys(c.detection)))] : [];
  const repairArms = res ? [...new Set(res.cases.flatMap((c) => Object.keys(c.repair)))] : [];
  return (
    <section className="rounded-2xl border border-line bg-panel shadow-panel">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3 text-xs text-muted">
        <h3 className="mr-1 text-sm font-semibold text-fg">Results</h3>
        <Pill tone={run.status === "finished" ? "ok" : run.status === "running" ? "accent" : "bad"}>{run.status}</Pill>
        <span>{fmtDateTime(run.created_at)}</span>
        {run.finished_at && <span>· {Math.round(run.finished_at - run.created_at)} s</span>}
        <span>· {run.config.case_ids.length} case(s)</span>
        {run.status === "running" && <Spinner className="h-3.5 w-3.5" />}
      </header>
      <div className="space-y-4 p-5">
        {res && (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {Object.entries(res.metrics).map(([k, m]) => {
              const ran = m.denominator > 0;
              return (
                <div key={k} className="flex items-center gap-3 rounded-xl border border-line bg-sunken/40 p-3">
                  {ran ? <Ring value={m.numerator} max={m.denominator} /> : <span className="flex h-11 w-11 items-center justify-center rounded-full border-2 border-dashed border-line-strong text-[10px] text-faint">n/a</span>}
                  <div className="min-w-0">
                    <div className="line-clamp-2 text-xs text-muted" title={m.label}>{m.label}</div>
                    {ran ? (
                      <div className={cx("text-xl font-semibold tabular-nums", m.numerator === m.denominator ? "text-ok" : "text-fg")}>
                        {m.numerator}<span className="text-sm text-faint">/{m.denominator}</span>
                      </div>
                    ) : <div className="text-sm font-semibold text-orange">not run</div>}
                    {m.not_run > 0 && <div className="text-[11px] text-faint">{m.not_run} not run</div>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {armRows.length > 0 && (
          <div>
            <div className="mb-2 text-xs font-semibold">Comparison <span className="font-normal text-faint">· cases each arm got right (detection: found the defect; repair: verifier-accepted patch)</span></div>
            <ul className="space-y-2">
              {armRows.map((a) => {
                const pct = a.ran ? (a.ok / a.ran) * 100 : 0;
                return (
                  <li key={a.arm} className="grid grid-cols-[minmax(9rem,14rem)_1fr_auto] items-center gap-3 text-xs">
                    <span className="truncate font-mono text-[11.5px]" title={a.arm}>{a.arm}</span>
                    <span className="h-2.5 overflow-hidden rounded-full bg-line/70">
                      {a.ran > 0 && <span className={cx("block h-full rounded-full", pct === 100 ? "bg-ok" : "sbx-brand-bg")} style={{ width: `${Math.max(pct, 2)}%` }} />}
                    </span>
                    <span className="w-28 text-right tabular-nums text-muted">{a.ran ? `${a.ok}/${a.ran}` : <span className="text-orange">not run</span>}{a.notRun && a.ran ? <span className="text-faint"> · {a.notRun} n/r</span> : null}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        {res && (
          <div className="overflow-x-auto rounded-xl border border-line">
            <table className="w-full min-w-[44rem] text-left text-xs">
              <thead className="border-b border-line bg-sunken/60 text-[11px] text-faint">
                <tr><th className="px-3 py-2 font-medium">Case</th><th className="px-2 font-medium">Harness</th>
                  {detectArms.map((a) => <th key={a} className="px-2 font-mono font-medium">{a}</th>)}
                  {repairArms.map((a) => <th key={a} className="px-2 font-mono font-medium">{a}</th>)}</tr>
              </thead>
              <tbody className="divide-y divide-line">
                {res.cases.map((c) => (
                  <tr key={c.case_id} className="align-top">
                    <td className="px-3 py-2">
                      <div className="font-semibold">{c.title}</div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-1"><Pill tone="violet">{KIND_LABEL[c.source_kind] ?? c.source_kind}</Pill>
                        <span className="text-[10.5px] text-faint">{c.cwe.join(", ")}</span></div>
                      <CaseDetails c={c} />
                    </td>
                    <td className="px-2 py-2">{c.harness ? verdictPill("ran", c.harness.vulnerable_fails && c.harness.reference_passes, "valid", "invalid",
                      `vulnerable ${c.harness.vulnerable_status}, reference ${c.harness.reference_status}`) : <Pill tone="idle">n/a</Pill>}</td>
                    {detectArms.map((a) => { const d = c.detection[a]; return <td key={a} className="px-2 py-2">{d ? verdictPill(d.status, d.correct, "found", "missed", d.reason) : "–"}</td>; })}
                    {repairArms.map((a) => { const r = c.repair[a]; return <td key={a} className="px-2 py-2">{r ? verdictPill(r.status, r.verdict ? r.verdict === "verified" : null, "verified", r.verdict ?? "failed", r.reason) : "–"}</td>; })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {res && Object.keys(res.tool_versions ?? {}).length > 0 && (
          <Collapsible title={<span className="text-xs">Advanced: tool versions</span>}>
            <p className="text-2xs text-muted">{Object.entries(res.tool_versions).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>
          </Collapsible>
        )}
      </div>
    </section>
  );
}

function verdictPill(status: string, ok: boolean | null, okLabel: string, badLabel: string, reason?: string) {
  if (status === "not_run") return <Pill tone="orange" title={reason}>not run</Pill>;
  if (status === "error") return <Pill tone="bad" title={reason}>error</Pill>;
  if (ok === null) return <Pill tone="idle">n/a</Pill>;
  return <Pill tone={ok ? "ok" : "bad"} title={reason}>{ok ? okLabel : badLabel}</Pill>;
}

/** Provenance, harness detail, per-arm reasons and the reproduce command for one case. */
function CaseDetails({ c }: { c: BenchCaseResult }) {
  const h = c.harness;
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-[11px] text-accent">Case details</summary>
      <div className="mt-1.5 max-w-xl space-y-1.5 text-[11px] text-muted">
        <p>{c.provenance}{c.repository ? ` · ${c.repository}@${c.commit}` : ""}</p>
        {h && (
          <p>vulnerable: {h.vulnerable_fails ? "fails security tests" : `passes (${h.vulnerable_status})`} · reference: {h.reference_passes ? "passes all tests" : h.reference_status}
            {" "}· formal <span className="font-mono">{h.formal_vulnerable ?? "–"} → {h.formal_reference ?? "–"}</span>
            {h.failed_properties.length > 0 ? ` · violated ${h.failed_properties.join(", ")}` : ""}</p>
        )}
        {Object.entries(c.detection).map(([arm, d]) => <p key={arm}><span className="font-mono">{arm}</span>: {d.status === "ran" ? `${d.findings} finding(s) · ${d.model ?? ""}${d.is_ai ? "" : " (not AI)"}` : d.reason}</p>)}
        {Object.entries(c.repair).map(([arm, r]) => <p key={arm}><span className="font-mono">{arm}</span>: {r.status === "ran" ? `${r.attempts} attempt(s) · ${r.verdict}` : r.reason}</p>)}
        <pre className="whitespace-pre-wrap rounded bg-sunken p-2 font-mono text-2xs">
{`curl -s -X POST http://127.0.0.1:8000/api/bench/runs -H 'content-type: application/json' \\
  -d '{"case_ids": ["${c.case_id}"]}'`}
        </pre>
        <p>{c.duration_s.toFixed(1)} s</p>
      </div>
    </details>
  );
}
