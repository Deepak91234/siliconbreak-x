/**
 * AI credit protection panel (GET /api/ai/budget, POST /api/ai/live).
 *
 * Live (billed) AI calls are off until the user turns them on here. Every call is
 * reserved at its worst-case cost and refused above the per-call, daily or total cap;
 * Droid sessions are capped per day. The numbers are the backend's ledger.
 */

import { useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type AIBudget, type AIProviderBudget } from "../api/security";
import { Bar, Button, ErrorNote, Pill, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";
import { toast } from "../lib/toast";
import { useResource } from "../lib/useResource";

export const usd = (v: number | null | undefined) => (v == null ? "–" : v < 0.01 && v > 0 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`);

export function useBudget(version: number | string = 0) {
  return useResource<AIBudget>("ai-budget", version, () => securityApi.aiBudget());
}

function ProviderRow({ label, p }: { label: string; p: AIProviderBudget }) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="font-medium text-fg">{label}</span>
        <span className="tabular-nums text-muted">{usd(p.used_usd)} of {usd(p.cap_total_usd)}</span>
      </div>
      <Bar value={p.used_usd} max={p.cap_total_usd || 1} tone={p.used_usd > p.cap_total_usd * 0.8 ? "warn" : "accent"} />
      <div className="text-2xs text-faint">
        {p.configured ? p.model : "no key"} · today {usd(p.used_today_usd)} / {usd(p.cap_daily_usd)} · per call ≤ {usd(p.cap_per_call_usd)}
        {" "}· worst case per audit call {usd(p.worst_case_per_call_usd)} · {p.calls} call{p.calls === 1 ? "" : "s"}
        {p.prices_are_estimates && " · prices are high placeholders"}
      </div>
    </div>
  );
}

export function AICredits({ compact = false }: { compact?: boolean }) {
  const [v, setV] = useState(0);
  const res = useBudget(v);
  const [busy, setBusy] = useState(false);
  const b = res.data;
  const toggle = async () => {
    if (!b) return;
    const enabling = !b.live_enabled;
    if (enabling) {
      const a = b.providers.anthropic, d = b.providers.bitdeer, f = b.providers.factory;
      const ok = window.confirm(
        "Turn on live AI calls?\n\n" +
        `Claude: up to ${usd(a.cap_total_usd)} total, ${usd(a.cap_daily_usd)} per day, ${usd(a.cap_per_call_usd)} per call.\n` +
        `Bitdeer: up to ${usd(d.cap_total_usd)} total, ${usd(d.cap_daily_usd)} per day, ${usd(d.cap_per_call_usd)} per call.\n` +
        `Factory Droid: at most ${f.cap_sessions_per_day} sessions per day, ${f.max_attempts_per_repair} per repair.\n\n` +
        "Audits make one investigation call; repairs and paid benchmark arms only run when you start them.");
      if (!ok) return;
    }
    setBusy(true);
    try {
      await securityApi.setLiveAI(enabling);
      setV((x) => x + 1);
      toast(enabling ? "Live AI calls are on (credit-guarded)." : "Live AI calls are off.", "info");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-label="AI credits" className="rounded-lg border border-line bg-panel">
      <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">AI credits</h3>
        <span className="flex items-center gap-2">
          {b && <Pill tone={b.live_enabled ? "ok" : "idle"}>{b.live_enabled ? "live AI on" : "live AI off"}</Pill>}
          <Button size="sm" variant={b?.live_enabled ? "secondary" : "primary"} busy={busy} disabled={!b} onClick={toggle}>
            {b?.live_enabled ? "Turn off" : "Turn on live AI"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setV((x) => x + 1)}>Refresh</Button>
        </span>
      </header>
      {res.error && <ErrorNote error={res.error} onRetry={res.reload} className="m-3" />}
      {b && (
        <div className="space-y-3 p-3">
          {!b.live_enabled && (
            <p className="text-2xs text-muted">Live calls are off: investigations use offline rules (not AI) and repairs other than the
              human reference are unavailable. Nothing is billed.</p>
          )}
          <div className={cx("grid gap-3", compact ? "" : "sm:grid-cols-2")}>
            <ProviderRow label="Claude (Anthropic)" p={b.providers.anthropic} />
            <ProviderRow label="Bitdeer" p={b.providers.bitdeer} />
          </div>
          <div className="text-xs">
            <span className="font-medium text-fg">Factory Droid</span>
            <span className="ml-2 tabular-nums text-muted">{b.providers.factory.sessions_today} / {b.providers.factory.cap_sessions_per_day} sessions today</span>
            <span className="ml-2 text-2xs text-faint">
              {b.providers.factory.configured ? b.providers.factory.model : "no key"} · ≤ {b.providers.factory.max_attempts_per_repair} per repair
              {b.providers.factory.credits_reported_total != null && ` · ${b.providers.factory.credits_reported_total} Factory credits reported`}
            </span>
          </div>
          {!compact && b.recent.length > 0 && (
            <details>
              <summary className="cursor-pointer text-2xs text-faint hover:text-fg">Recent AI calls ({b.recent.length})</summary>
              <ul className="mt-1 space-y-0.5 text-2xs">
                {b.recent.map((r, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="w-28 shrink-0 text-faint">{fmtDateTime(r.ts)}</span>
                    <span className="w-16 shrink-0">{r.provider}</span>
                    <span className="min-w-0 flex-1 truncate text-muted" title={r.detail?.reason ?? ""}>
                      {r.purpose} · {r.status}{r.detail?.reason ? `: ${r.detail.reason}` : ""}
                    </span>
                    <span className="w-16 shrink-0 text-right tabular-nums">{r.provider === "factory" ? "session" : usd(r.cost_usd ?? r.max_cost_usd)}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </section>
  );
}
