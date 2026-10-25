/**
 * Primary navigation: brand, the four areas, Settings & usage. Collapses to an
 * icon rail on narrow screens or on request (persisted).
 */

import type { ReactNode } from "react";
import { BrandMark, BrandName } from "../components/brand";
import { IconBars, IconChevronLeft, IconChevronRight, IconChip, IconCode, IconCoins, IconShield } from "../components/icons";
import { cx } from "../components/ui";
import { SECTION_INFO, sectionOf, type Section } from "../lib/sections";
import { setUi } from "../lib/ui";
import { useAppState, useStore } from "../store/store";

const ICONS: Record<Section, ReactNode> = {
  security: <IconShield size={18} />,
  bench: <IconBars size={18} />,
  fpga: <IconChip size={18} />,
  workspace: <IconCode size={18} />,
};

const ORDER: Section[] = ["security", "bench", "fpga", "workspace"];

export function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const store = useStore();
  const tab = useAppState((s) => s.selection.tab);
  const section = sectionOf(tab);

  const go = (s: Section) => {
    if (s === "workspace") store.setTab(sectionOf(tab) === "workspace" ? tab : "pipeline");
    else store.setTab(SECTION_INFO[s].tab);
  };

  const item = (key: string, label: string, blurb: string, icon: ReactNode, active: boolean, onClick: () => void) => (
    <button type="button" onClick={onClick} aria-current={active ? "page" : undefined} title={collapsed ? label : undefined}
            className={cx("group relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-all duration-150",
              collapsed && "justify-center px-0",
              active ? "bg-accent/10 text-fg ring-1 ring-inset ring-accent/30 shadow-[0_0_24px_-10px_rgb(var(--accent)/0.8)]" : "text-muted hover:bg-raised hover:text-fg")}
            key={key}>
      {active && <span className="sbx-brand-bg absolute inset-y-2 left-0 w-[3px] rounded-r-full" aria-hidden />}
      <span className={cx("shrink-0 transition-colors", active ? "text-accent" : "text-faint group-hover:text-fg")}>{icon}</span>
      {!collapsed && (
        <span className="min-w-0">
          <span className="block text-[13.5px] font-semibold">{label}</span>
          <span className="block truncate text-[11px] text-faint">{blurb}</span>
        </span>
      )}
    </button>
  );

  return (
    <nav aria-label="Primary" className={cx("flex h-full flex-none flex-col overflow-hidden whitespace-nowrap border-r border-line bg-panel/80 backdrop-blur transition-[width] duration-200",
      collapsed ? "w-[68px]" : "w-[236px]")}>
      <div className={cx("flex items-center gap-2.5 px-4 pb-4 pt-4", collapsed && "justify-center px-0")}>
        <BrandMark size={34} />
        {!collapsed && (
          <div className="min-w-0 leading-tight">
            <BrandName className="text-[17px]" />
            <div className="text-[11px] text-muted">AI hardware security</div>
          </div>
        )}
      </div>
      <div className={cx("space-y-1 px-3", collapsed && "px-2")}>
        {ORDER.map((s) => item(s, SECTION_INFO[s].label, SECTION_INFO[s].blurb, ICONS[s], section === s, () => go(s)))}
      </div>
      <div className="flex-1" />
      <div className={cx("px-3 pb-2", collapsed && "px-2")}>
        {item("settings", "Settings & usage", "AI providers, credits", <IconCoins size={18} />, false, () => setUi({ settings: true }))}
      </div>
      <button type="button" onClick={onToggle} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              className="flex h-9 items-center justify-center gap-1.5 border-t border-line text-[11px] text-faint transition-colors hover:bg-raised hover:text-fg">
        {collapsed ? <IconChevronRight size={14} /> : <><IconChevronLeft size={14} /> Collapse</>}
      </button>
    </nav>
  );
}
