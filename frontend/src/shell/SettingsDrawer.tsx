/**
 * Settings & usage: AI provider configuration, the credit-protection ledger with
 * the live-AI switch (GET /api/ai/budget, POST /api/ai/live), and provider/tool
 * health. The only place these appear, so no page repeats them.
 */

import { useEffect } from "react";
import { IconBot, IconX } from "../components/icons";
import { Button } from "../components/ui";
import { setUi } from "../lib/ui";
import { AICredits } from "../security/AICredits";
import { ProviderStatus } from "../security/ProviderStatus";

export function SettingsDrawer({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 flex bg-black/50" onClick={onClose} role="presentation">
      <aside role="dialog" aria-label="Settings and usage" onClick={(e) => e.stopPropagation()}
             className="sbx-enter ml-auto flex h-full w-[min(36rem,96vw)] flex-col border-l border-line bg-bg shadow-pop">
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <h2 className="text-[15px] font-semibold">Settings & usage</h2>
          <button type="button" onClick={onClose} className="ml-auto rounded-lg p-1.5 text-muted hover:bg-raised hover:text-fg" aria-label="Close settings">
            <IconX size={16} />
          </button>
        </header>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
          <section className="flex items-center gap-3 rounded-xl border border-line bg-panel p-4 shadow-panel">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/10 text-accent"><IconBot size={18} /></span>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold">AI provider</div>
              <p className="text-xs text-muted">Model used by the Workspace AI builder; keys stay on the backend.</p>
            </div>
            <Button size="sm" variant="secondary" onClick={() => setUi({ aiDialog: true })}>Configure</Button>
          </section>
          <AICredits />
          <ProviderStatus />
        </div>
      </aside>
    </div>
  );
}
