/**
 * SiliconBreak X dependency health (GET /api/security/health). Shows only
 * states the backend reports; no key material ever reaches the browser.
 */

import { useState } from "react";
import { securityApi, type SecurityHealth } from "../api/security";
import { Button, ErrorNote, Pill, Spinner, cx } from "../components/ui";
import { healthState } from "../lib/status";
import { useResource } from "../lib/useResource";

const ORDER: Array<["providers" | "tools", string, string]> = [
  ["providers", "bitdeer", "Bitdeer"],
  ["providers", "anthropic", "Claude"],
  ["providers", "factory", "Factory Droid"],
  ["providers", "rules", "Offline rules"],
  ["tools", "verilator", "Verilator"],
  ["tools", "yosys", "Yosys formal"],
  ["tools", "vivado", "Vivado"],
  ["tools", "fpga", "Basys 3"],
];

export function ProviderStatus({ compact = false }: { compact?: boolean }) {
  const [nonce, setNonce] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const res = useResource<SecurityHealth>("health", nonce, () => securityApi.health(nonce > 0));
  const h = res.data;
  return (
    <section aria-label="Provider and tool status" className="rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex items-center justify-between border-b border-line px-3 py-2">
        <h3 className="text-sm font-semibold text-fg">Providers and tools</h3>
        <Button size="sm" variant="ghost" busy={refreshing}
                onClick={async () => { setRefreshing(true); setNonce((n) => n + 1); setTimeout(() => setRefreshing(false), 800); }}>
          Recheck
        </Button>
      </header>
      {res.error && <ErrorNote error={res.error} onRetry={res.reload} className="m-3" />}
      {!h && !res.error && <div className="flex items-center gap-2 p-3 text-xs text-muted"><Spinner className="h-3.5 w-3.5" /> Checking…</div>}
      {h && (
        <ul className={cx("grid gap-px bg-line", compact ? "grid-cols-2" : "sm:grid-cols-2")}>
          {ORDER.map(([group, key, label]) => {
            const it = (h[group] as Record<string, { state: string; detail: string; model?: string; role?: string }>)[key];
            if (!it) return null;
            const st = healthState(it.state);
            return (
              <li key={key} className="flex items-start gap-2 bg-panel px-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-fg">{label}</span>
                    <Pill tone={st.tone}>{st.label}</Pill>
                  </div>
                  {!compact && <p className="mt-0.5 line-clamp-2 text-2xs text-muted" title={it.detail}>
                    {it.role ? `${it.role}. ` : ""}{it.model ? `${it.model}. ` : ""}{it.detail}
                  </p>}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {h && !compact && (
        <p className="border-t border-line px-3 py-2 text-2xs text-faint">
          Investigator routing: <span className="font-mono">{h.routing.investigator}</span>. Without a model key, investigation
          uses offline static rules and every result is labelled “not AI”. Keys go in <span className="font-mono">.env</span> on
          the backend (BITDEER_API_KEY, ANTHROPIC_API_KEY, FACTORY_API_KEY); restart the backend after changing them.
        </p>
      )}
    </section>
  );
}
