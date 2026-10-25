/**
 * Primary navigation: brand, the five sections, recent audits and designs (from
 * the store), and the AI credit meters (GET /api/ai/budget). Collapses to an icon
 * rail on narrow screens or on request (persisted).
 */

import { useMemo, type ReactNode } from "react";
import { BrandMark, BrandName } from "../components/brand";
import { IconBars, IconChevronLeft, IconChevronRight, IconChip, IconCode, IconGrid, IconShield } from "../components/icons";
import { Dot, cx } from "../components/ui";
import { fmtAgo } from "../lib/format";
import { SECTION_INFO, isAuditProject, sectionOf, type Section } from "../lib/sections";
import { runStatus } from "../lib/status";
import { useNow } from "../lib/useResource";
import { auditHeadlineTone } from "../security/derive";
import { usd, useBudget } from "../security/AICredits";
import { useAppState, useStore } from "../store/store";

const ICONS: Record<Section, ReactNode> = {
  dashboard: <IconGrid size={18} />,
  security: <IconShield size={18} />,
  bench: <IconBars size={18} />,
  fpga: <IconChip size={18} />,
  workspace: <IconCode size={18} />,
};

const ORDER: Section[] = ["dashboard", "security", "bench", "fpga", "workspace"];

export function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const store = useStore();
  const tab = useAppState((s) => s.selection.tab);
  const auditId = useAppState((s) => s.selection.auditId);
  const projectId = useAppState((s) => s.selection.projectId);
  const security = useAppState((s) => s.security);
  const projects = useAppState((s) => s.projects);
  const now = useNow(30_000);
  const section = sectionOf(tab);

  const audits = useMemo(() => Object.values(security).sort((a, b) => b.created_at - a.created_at).slice(0, 4), [security]);
  const designs = useMemo(() => (projects ?? []).filter((p) => !isAuditProject(p.title)).slice(0, 3), [projects]);

  const go = (s: Section) => {
    if (s === "workspace") store.setTab(sectionOf(tab) === "workspace" ? tab : "pipeline");
    else store.setTab(SECTION_INFO[s].tab);
  };

  return (
    <nav aria-label="Primary" className={cx("flex h-full flex-none flex-col overflow-hidden whitespace-nowrap border-r border-line bg-panel/80 backdrop-blur transition-[width] duration-200",
      collapsed ? "w-[68px]" : "w-[248px]")}>
      <div className={cx("flex items-center gap-2.5 px-4 pb-3 pt-4", collapsed && "justify-center px-0")}>
        <BrandMark size={34} />
        {!collapsed && (
          <div className="min-w-0 leading-tight">
            <BrandName className="text-[17px]" />
            <div className="text-[11px] text-muted">AI hardware security</div>
          </div>
        )}
      </div>

      <ul className={cx("space-y-1 px-3", collapsed && "px-2")}>
        {ORDER.map((s) => {
          const active = section === s;
          return (
            <li key={s}>
              <button type="button" onClick={() => go(s)} aria-current={active ? "page" : undefined} title={collapsed ? SECTION_INFO[s].label : undefined}
                      className={cx("group relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-all duration-150",
                        collapsed && "justify-center px-0",
                        active ? "bg-accent/10 text-fg ring-1 ring-inset ring-accent/30 shadow-[0_0_24px_-10px_rgb(var(--accent)/0.8)]" : "text-muted hover:bg-raised hover:text-fg")}>
                {active && <span className="absolute inset-y-2 left-0 w-[3px] rounded-r-full sbx-brand-bg" aria-hidden />}
                <span className={cx("shrink-0 transition-colors", active ? "text-accent" : "text-faint group-hover:text-fg")}>{ICONS[s]}</span>
                {!collapsed && (
                  <span className="min-w-0">
                    <span className="block text-[13.5px] font-semibold">{SECTION_INFO[s].label}</span>
                    <span className="block truncate text-[11px] text-faint">{SECTION_INFO[s].blurb}</span>
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>

      <div className="mt-4 min-h-0 flex-1 overflow-y-auto px-3">
        {!collapsed && audits.length > 0 && (
          <div className="mb-4">
            <div className="mb-1.5 px-2 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-faint">Recent audits</div>
            <ul className="space-y-0.5">
              {audits.map((a) => {
                const st = runStatus(a.status);
                const active = section !== "workspace" && auditId === a.id;
                return (
                  <li key={a.id}>
                    <button type="button" onClick={() => store.selectAudit(a.id)}
                            className={cx("flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-raised", active && "bg-raised")}>
                      <Dot tone={auditHeadlineTone(a)} pulse={a.status === "running"} className="mt-1.5" />
                      <span className="min-w-0">
                        <span className="block truncate text-xs font-medium text-fg">{a.record.case?.title ?? a.record.source?.label ?? a.id}</span>
                        <span className="block truncate text-[11px] text-faint">{st.label} · {fmtAgo(a.created_at, now)}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        {!collapsed && designs.length > 0 && (
          <div className="mb-4">
            <div className="mb-1.5 px-2 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-faint">Recent designs</div>
            <ul className="space-y-0.5">
              {designs.map((p) => (
                <li key={p.id}>
                  <button type="button" onClick={() => { store.selectProject(p.id); store.setTab(sectionOf(tab) === "workspace" ? tab : "pipeline"); }}
                          className={cx("flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-raised",
                            section === "workspace" && projectId === p.id && "bg-raised")}>
                    <Dot tone={runStatus(p.latest_run_status).tone} className="mt-1.5" />
                    <span className="min-w-0">
                      <span className="block truncate text-xs font-medium text-fg">{p.title}</span>
                      <span className="block truncate text-[11px] text-faint">design · {fmtAgo(p.updated_at, now)}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {!collapsed && <CreditsMini />}
      <button type="button" onClick={onToggle} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              className="flex h-9 items-center justify-center gap-1.5 border-t border-line text-[11px] text-faint transition-colors hover:bg-raised hover:text-fg">
        {collapsed ? <IconChevronRight size={14} /> : <><IconChevronLeft size={14} /> Collapse</>}
      </button>
    </nav>
  );
}

/** Credit meters: share of each provider's total cap used, from the backend ledger. */
function CreditsMini() {
  const store = useStore();
  const res = useBudget(0);
  const b = res.data;
  if (!b) return null;
  const rows: Array<{ label: string; used: number; cap: number; text: string }> = [
    { label: "Claude", used: b.providers.anthropic.used_usd, cap: b.providers.anthropic.cap_total_usd, text: `${usd(b.providers.anthropic.used_usd)} / ${usd(b.providers.anthropic.cap_total_usd)}` },
    { label: "Bitdeer", used: b.providers.bitdeer.used_usd, cap: b.providers.bitdeer.cap_total_usd, text: `${usd(b.providers.bitdeer.used_usd)} / ${usd(b.providers.bitdeer.cap_total_usd)}` },
    { label: "Factory Droid", used: b.providers.factory.sessions_today, cap: b.providers.factory.cap_sessions_per_day, text: `${b.providers.factory.sessions_today} / ${b.providers.factory.cap_sessions_per_day} today` },
  ];
  return (
    <button type="button" onClick={() => store.selectAudit(null)} title="Credit protection settings (Security Lab)"
            className="mx-3 mb-3 rounded-xl border border-line bg-sunken/60 p-3 text-left transition-colors hover:border-accent/40">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold text-fg">AI credits</span>
        <span className={cx("rounded-full px-1.5 py-px text-[10px] font-semibold", b.live_enabled ? "bg-ok/15 text-ok" : "bg-raised text-faint")}>
          {b.live_enabled ? "live on" : "live off"}
        </span>
      </div>
      <div className="space-y-1.5">
        {rows.map((r) => {
          const pct = r.cap > 0 ? Math.min(100, (r.used / r.cap) * 100) : 0;
          return (
            <div key={r.label}>
              <div className="flex justify-between text-[11px]"><span className="text-muted">{r.label}</span><span className="tabular-nums text-faint">{r.text}</span></div>
              <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-line/70">
                <div className={cx("h-full rounded-full", pct > 80 ? "bg-warn" : "sbx-brand-bg")} style={{ width: `${Math.max(pct, r.used > 0 ? 2 : 0)}%` }} />
              </div>
            </div>
          );
        })}
      </div>
    </button>
  );
}
