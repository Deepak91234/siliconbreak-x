/**
 * Minimal toast queue for the outcome of user actions (e.g. "Rebuild started",
 * or the server's error detail when an action is refused).
 */

export type ToastKind = "info" | "ok" | "error";

export interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

type Listener = (toasts: Toast[]) => void;

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<Listener>();

function emit() {
  for (const l of listeners) l(toasts);
}

export function toast(text: string, kind: ToastKind = "info", ttlMs = kind === "error" ? 8000 : 4000): void {
  const t: Toast = { id: nextId++, kind, text };
  toasts = [...toasts.slice(-4), t];
  emit();
  setTimeout(() => dismissToast(t.id), ttlMs);
}

export function dismissToast(id: number): void {
  const next = toasts.filter((t) => t.id !== id);
  if (next.length !== toasts.length) {
    toasts = next;
    emit();
  }
}

export function subscribeToasts(fn: Listener): () => void {
  listeners.add(fn);
  fn(toasts);
  return () => listeners.delete(fn);
}
