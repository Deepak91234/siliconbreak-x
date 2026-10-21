import { useEffect, useState } from "react";
import { dismissToast, subscribeToasts, type Toast } from "../lib/toast";
import { cx } from "./ui";

export function Toasts() {
  const [items, setItems] = useState<Toast[]>([]);
  useEffect(() => subscribeToasts(setItems), []);
  if (!items.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-2">
      {items.map((t) => (
        <div
          key={t.id}
          role="status"
          className={cx(
            "pointer-events-auto flex items-start gap-2 rounded-lg border px-3 py-2 text-xs shadow-lg shadow-black/30 backdrop-blur",
            t.kind === "error" ? "border-bad/40 bg-panel text-bad" : t.kind === "ok" ? "border-ok/40 bg-panel text-ok" : "border-line bg-panel text-fg",
          )}
        >
          <span className="min-w-0 flex-1 break-words">{t.text}</span>
          <button type="button" className="text-muted hover:text-fg" onClick={() => dismissToast(t.id)} aria-label="Dismiss">
            &times;
          </button>
        </div>
      ))}
    </div>
  );
}
