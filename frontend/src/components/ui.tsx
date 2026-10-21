/**
 * Small presentational primitives shared by every panel. Styling lives here so
 * the panels read as data -> layout without repeating class soup.
 */

import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { TONE_DOT, TONE_PILL, type Tone } from "../lib/status";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "ok" | "warn";

const BTN: Record<ButtonVariant, string> = {
  primary: "bg-accent text-slate-950 hover:bg-accent/90 font-semibold",
  secondary: "bg-raised text-fg ring-1 ring-line hover:bg-line/60",
  ghost: "text-muted hover:text-fg hover:bg-raised",
  danger: "bg-bad/15 text-bad ring-1 ring-bad/30 hover:bg-bad/25",
  ok: "bg-ok text-slate-950 hover:bg-ok/90 font-semibold",
  warn: "bg-warn/15 text-warn ring-1 ring-warn/30 hover:bg-warn/25",
};

export function Button({
  variant = "secondary",
  size = "md",
  busy = false,
  className,
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: "sm" | "md"; busy?: boolean }) {
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || busy}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-8 px-3 text-sm",
        BTN[variant],
        className,
      )}
    >
      {busy && <Spinner className="h-3.5 w-3.5" />}
      {children}
    </button>
  );
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={cx("animate-spin", className)} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function Pill({ tone, children, className, title }: { tone: Tone; children: ReactNode; className?: string; title?: string }) {
  return (
    <span
      title={title}
      className={cx("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset", TONE_PILL[tone], className)}
    >
      {children}
    </span>
  );
}

export function Dot({ tone, pulse = false, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return <span className={cx("inline-block h-2 w-2 shrink-0 rounded-full", TONE_DOT[tone], pulse && "animate-pulse-dot", className)} />;
}

export function Card({ title, actions, children, className, bodyClassName }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cx("rounded-lg border border-line bg-panel", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
          <h3 className="min-w-0 truncate text-xs font-semibold uppercase tracking-wide text-muted">{title}</h3>
          {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cx("p-3", bodyClassName)}>{children}</div>
    </section>
  );
}

/** Explicit "nothing here yet" state. Every panel uses it instead of blank space. */
export function Empty({ title, children, icon, className }: { title: string; children?: ReactNode; icon?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line px-4 py-8 text-center", className)}>
      {icon ?? (
        <svg viewBox="0 0 24 24" className="h-6 w-6 text-faint" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
          <rect x="4" y="4" width="16" height="16" rx="3" />
          <path d="M8 12h8" strokeLinecap="round" />
        </svg>
      )}
      <div className="text-sm font-medium text-fg">{title}</div>
      {children && <div className="max-w-md text-xs text-muted">{children}</div>}
    </div>
  );
}

export function ErrorNote({ error, onRetry, className }: { error: unknown; onRetry?: () => void; className?: string }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className={cx("flex items-start gap-2 rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad", className)}>
      <span className="min-w-0 flex-1 break-words">{msg}</span>
      {onRetry && (
        <button type="button" className="shrink-0 underline hover:no-underline" onClick={onRetry}>
          retry
        </button>
      )}
    </div>
  );
}

export function Loading({ label = "Loading…", className }: { label?: string; className?: string }) {
  return (
    <div className={cx("flex items-center gap-2 px-1 py-4 text-xs text-muted", className)}>
      <Spinner className="h-3.5 w-3.5" /> {label}
    </div>
  );
}

export function KV({ items, className }: { items: Array<[ReactNode, ReactNode]>; className?: string }) {
  return (
    <dl className={cx("grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs", className)}>
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Metric({ label, value, tone, sub }: { label: string; value: ReactNode; tone?: Tone; sub?: ReactNode }) {
  const toneText = tone ? { ok: "text-ok", bad: "text-bad", warn: "text-warn", info: "text-info", idle: "text-muted", accent: "text-accent", violet: "text-violet", orange: "text-orange", muted: "text-muted" }[tone] : "text-fg";
  return (
    <div className="rounded-md border border-line bg-sunken/60 px-3 py-2">
      <div className="text-[10px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className={cx("mt-0.5 font-mono text-base tabular-nums", toneText)}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
    </div>
  );
}

export function Collapsible({ title, children, defaultOpen = false, right }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean; right?: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-md border border-line">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-raised/60">
        <Chevron open={open} />
        <span className="min-w-0 flex-1 truncate font-medium">{title}</span>
        {right}
      </button>
      {open && <div className="border-t border-line p-3">{children}</div>}
    </div>
  );
}

export function Chevron({ open, className }: { open: boolean; className?: string }) {
  return (
    <svg viewBox="0 0 16 16" className={cx("h-3 w-3 shrink-0 text-muted transition-transform", open && "rotate-90", className)} fill="currentColor" aria-hidden>
      <path d="M6 3l5 5-5 5z" />
    </svg>
  );
}

export function Pre({ children, className, maxH = "max-h-80" }: { children: ReactNode; className?: string; maxH?: string }) {
  return (
    <pre className={cx("overflow-auto whitespace-pre rounded-md bg-sunken p-2 font-mono text-[11px] leading-relaxed text-fg/90", maxH, className)}>
      {children}
    </pre>
  );
}

export function Segmented<T extends string>({ value, options, onChange, size = "sm" }: {
  value: T;
  options: Array<{ value: T; label: ReactNode; title?: string }>;
  onChange: (v: T) => void;
  size?: "sm" | "md";
}) {
  return (
    <div className="inline-flex rounded-md bg-sunken p-0.5 ring-1 ring-line">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded px-2 transition-colors",
            size === "sm" ? "h-6 text-[11px]" : "h-7 text-xs",
            value === o.value ? "bg-raised text-fg shadow-sm ring-1 ring-line" : "text-muted hover:text-fg",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Select({ value, onChange, children, className, disabled, title }: {
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <select
      value={value}
      title={title}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={cx(
        "h-7 rounded-md border border-line bg-sunken px-2 text-xs text-fg outline-none focus:border-accent disabled:opacity-50",
        className,
      )}
    >
      {children}
    </select>
  );
}

/** Horizontal usage bar (utilization, phase progress). */
export function Bar({ value, max = 100, tone = "accent", className }: { value: number; max?: number; tone?: Tone; className?: string }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className={cx("h-1.5 w-full overflow-hidden rounded-full bg-sunken ring-1 ring-line", className)}>
      <div className={cx("h-full rounded-full transition-[width] duration-300", TONE_DOT[tone])} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function CopyButton({ text, label = "copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="text-[11px] text-muted underline-offset-2 hover:text-fg hover:underline"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        });
      }}
    >
      {done ? "copied" : label}
    </button>
  );
}
