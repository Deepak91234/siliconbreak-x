/**
 * SiliconBench: stored benchmark runs over the security cases (GET/POST /api/bench/runs).
 * Every number is computed by the backend from executed records; denominators and
 * not-run counts are always shown, and a provider without a key is NOT RUN, never 0%.
 */

import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type BenchCaseResult, type BenchEstimate, type BenchRun } from "../api/security";
import { IconBars } from "../components/icons";
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

  const paidSel = ARMS.filter((a) => a.paid && arms.includes(a.id)).length;
  return (
    <div className="space-y-5">
      <section className="sbx-hero rounded-2xl border border-line p-6 shadow-panel">
        <div className="flex flex-wrap items-start gap-4">
          <div className="min-w-0 flex-1">
            <div className="text-[10.5px] font-semibold uppercase tracking-[0.08em] text-accent">SiliconBench</div>
            <h2 className="mt-1 text-3xl font-semibold tracking-tight">Benchmark <span className="sbx-gradient-text">AI security agents</span> on the same verifier</h2>
            <p className="mt-2 max-w-3xl text-sm text-muted">
              Every case runs through one trusted harness: harness validity, detection by each investigator, and repair by a single-shot
              model, the same model with the verifier loop, and Factory Droid. Arms without a configured provider are reported as
              <span className="font-medium text-orange"> not run</span>, never as 0%. Demo and locally authored cases are labelled and are not
              historical-vulnerability results.
            </p>
          </div>
          <div className="flex flex-col items-end gap-2">
            {runs && runs.length > 1 && (
              <Select value={run?.id ?? ""} onChange={setPicked} className="max-w-[16rem]">
                {runs.map((r) => <option key={r.id} value={r.id}>{fmtDateTime(r.created_at)} · {r.status}</option>)}
              </Select>
            )}
            <Button variant="primary" busy={starting} disabled={run?.status === "running" || arms.length === 0} onClick={start}>
              Run SiliconBench{est?.paid ? ` (≤ ${usd(est.usd_total)}${est.droid_sessions ? `, ${est.droid_sessions} Droid` : ""})` : " (free)"}
            </Button>
          </div>
        </div>
        <div className="mt-5 grid gap-3 lg:grid-cols-2">
          {([["Free arms", false], ["Paid arms", true]] as const).map(([title, paid]) => (
            <fieldset key={title} className="rounded-xl border border-line bg-panel/70 p-3 backdrop-blur" aria-label={title}>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-semibold">{title}</span>
                {paid && <span className="text-[11px] text-faint">{paidSel} selected · each confirms its worst case first</span>}
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
          <p className="mt-3 text-xs text-muted">
            Worst case for this selection: <span className="font-semibold text-fg">{usd(est.usd_total)}</span> (Claude {usd(est.usd.anthropic)}, Bitdeer {usd(est.usd.bitdeer)}),
            {" "}{est.droid_sessions} Droid session(s). The credit guard still refuses any call above its caps.
          </p>
        )}
      </section>
      {err && <ErrorNote error={err} onRetry={load} />}
      {!runs && !err && <div className="flex items-center gap-2 text-xs text-muted"><Spinner className="h-3.5 w-3.5" /> Loading…</div>}
      {runs && !run && <Empty title="No benchmark runs yet" icon={<IconBars size={20} />}>Press “Run SiliconBench” to execute every case. The free arms (harness and offline rules) cost nothing; results are stored and can be compared later.</Empty>}
      {run && <BenchResult run={run} />}
      <AICredits compact />
    </div>
  );
}

function Ring({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? value / max : 0;
  const r = 20, c = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 48 48" className="h-12 w-12 -rotate-90" aria-hidden>
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
    if (!res) return [] as Array<{ arm: string; kind: "detect" | "repair"; ok: number; ran: number; notRun: number }>;
    const m = new Map<string, { arm: string; kind: "detect" | "repair"; ok: number; ran: number; notRun: number }>();
    for (const c of res.cases) {
      for (const [arm, d] of Object.entries(c.detection)) {
        const e = m.get(arm) ?? { arm, kind: "detect" as const, ok: 0, ran: 0, notRun: 0 };
        if (d.status === "ran") { e.ran++; if (d.correct) e.ok++; } else e.notRun++;
        m.set(arm, e);
      }
      for (const [arm, r] of Object.entries(c.repair)) {
        const e = m.get(arm) ?? { arm, kind: "repair" as const, ok: 0, ran: 0, notRun: 0 };
        if (r.status === "ran") { e.ran++; if (r.verdict === "verified") e.ok++; } else e.notRun++;
        m.set(arm, e);
      }
    }
    return [...m.values()];
  }, [res]);
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Pill tone={run.status === "finished" ? "ok" : run.status === "running" ? "accent" : "bad"}>{run.status}</Pill>
        <span>started {fmtDateTime(run.created_at)}</span>
        {run.finished_at && <span>· {Math.round(run.finished_at - run.created_at)} s</span>}
        <span>· {run.config.case_ids.length} case(s) · arms: {run.config.arms.join(", ")}</span>
        {run.status === "running" && <Spinner className="h-3.5 w-3.5" />}
      </div>
      {res && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {Object.entries(res.metrics).map(([k, m]) => {
            const ran = m.denominator > 0;
            return (
              <div key={k} className="flex items-center gap-3 rounded-xl border border-line bg-panel p-4 shadow-panel">
                {ran ? <Ring value={m.numerator} max={m.denominator} /> : <span className="flex h-12 w-12 items-center justify-center rounded-full border-2 border-dashed border-line-strong text-[10px] text-faint">n/a</span>}
                <div className="min-w-0">
                  <div className="line-clamp-2 text-xs text-muted" title={m.label}>{m.label}</div>
                  {ran ? (
                    <div className={cx("text-2xl font-semibold tabular-nums", m.numerator === m.denominator ? "text-ok" : "text-fg")}>
                      {m.numerator}<span className="text-base text-faint">/{m.denominator}</span>
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
        <Card title="Arm comparison" subtitle="share of cases each arm got right (detection: found the defect; repair: verifier-accepted patch)" icon={<IconBars size={15} />}>
          <ul className="space-y-2.5">
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
