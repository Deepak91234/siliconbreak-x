/**
 * AI provider configuration. The API key is configured on the backend only
 * (environment or the repository's .env file); this dialog shows which mode is
 * active, how to enable the model, and asks the backend to re-read its config.
 * It never asks for, receives or displays the key.
 */

import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { LlmStatus } from "../api/types";
import { Button, CopyButton, Pill, Spinner } from "./ui";

export function AIProviderDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [status, setStatus] = useState<LlmStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    api.llm().then(setStatus, (e) => setError(errorMessage(e)));
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const reload = async () => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await api.llmReload());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const model = status?.mode === "model";
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="ai-dialog-title"
           className="w-full max-w-lg rounded-lg border border-line bg-panel p-5 shadow-pop outline-none" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 id="ai-dialog-title" className="text-base font-semibold">AI agent mode</h2>
            <p className="mt-0.5 text-xs text-muted">Which engine interprets requests and writes or repairs Verilog.</p>
          </div>
          <button type="button" onClick={onClose} className="text-muted hover:text-fg" aria-label="Close">×</button>
        </div>

        {!status && !error && <div className="mt-4 text-sm text-muted"><Spinner className="mr-2 inline h-4 w-4" />Checking…</div>}
        {status && (
          <div className="mt-4 space-y-4 text-sm">
            <div className="flex items-center gap-2">
              <Pill tone={model ? "ok" : "warn"}>{model ? "Model-powered agent" : "Rules-based offline fallback"}</Pill>
              {model && <span className="font-mono text-xs text-muted">{status.provider} · {status.model}</span>}
            </div>
            <p className="text-xs leading-relaxed text-muted">{status.explanation}</p>
            {!model && (
              <ol className="list-decimal space-y-2 pl-5 text-xs text-muted">
                <li>
                  Open the backend's environment file:
                  <div className="mt-1 flex items-center gap-2 rounded bg-sunken px-2 py-1 font-mono text-2xs text-fg">
                    <span className="min-w-0 flex-1 truncate">{status.env_file}</span><CopyButton text={status.env_file} />
                  </div>
                  {!status.env_file_exists && <span className="text-faint">It does not exist yet: copy .env.example to .env.</span>}
                </li>
                <li>Add <code className="rounded bg-sunken px-1 font-mono text-fg">ANTHROPIC_API_KEY=…</code> there. Do not paste the key into this page; it stays on the backend.</li>
                <li>Reload the configuration (no restart needed):</li>
              </ol>
            )}
            {model && status.key_source && <p className="text-xs text-faint">Key read from the {status.key_source}. It is never sent to the browser.</p>}
            <div className="flex items-center gap-2">
              <Button size="sm" variant={model ? "secondary" : "primary"} busy={busy} onClick={() => void reload()}>Reload configuration</Button>
              <span className="text-2xs text-faint">Re-reads .env and the environment on the backend.</span>
            </div>
          </div>
        )}
        {error && <p className="mt-3 text-xs text-bad">{error}</p>}
      </div>
    </div>
  );
}
