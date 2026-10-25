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
  primary: "sbx-btn-primary text-white font-semibold",
  secondary: "bg-panel text-fg ring-1 ring-line shadow-sm hover:bg-raised hover:ring-line-strong",
  ghost: "text-muted hover:text-fg hover:bg-raised",
  danger: "bg-bad/10 text-bad ring-1 ring-bad/30 hover:bg-bad/20",
  ok: "bg-ok text-white hover:bg-ok/90 font-semibold shadow-sm",
  warn: "bg-warn/10 text-warn ring-1 ring-warn/30 hover:bg-warn/20",
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
        "inline-flex select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-lg font-medium transition-all duration-150 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100",
        size === "sm" ? "h-8 px-3 text-xs" : "h-9 px-3.5 text-sm",
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
      className={cx("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset", TONE_PILL[tone], className)}
    >
      {children}
    </span>
  );
}

export function Dot({ tone, pulse = false, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return <span className={cx("inline-block h-2 w-2 shrink-0 rounded-full", TONE_DOT[tone], pulse && "animate-pulse-dot", className)} />;
}

export function Card({ title, actions, children, className, bodyClassName, icon, subtitle }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  icon?: ReactNode;
  subtitle?: ReactNode;
}) {
  return (
    <section className={cx("rounded-xl border border-line bg-panel shadow-panel", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2.5">
            {icon && <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent/10 text-accent">{icon}</span>}
            <div className="min-w-0">
              <h3 className="truncate text-sm font-semibold text-fg">{title}</h3>
              {subtitle && <div className="truncate text-2xs text-muted">{subtitle}</div>}
            </div>
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cx("p-4", bodyClassName)}>{children}</div>
    </section>
  );
}

/** Icon in a tinted rounded square (stat tiles, stage cards, list rows). */
export function IconBadge({ tone = "accent", children, size = "md", className }: { tone?: Tone; children: ReactNode; size?: "sm" | "md" | "lg"; className?: string }) {
  return (
    <span className={cx("flex shrink-0 items-center justify-center rounded-xl ring-1 ring-inset", TONE_PILL[tone],
      size === "sm" ? "h-7 w-7" : size === "lg" ? "h-11 w-11" : "h-9 w-9", className)}>
      {children}
    </span>
  );
}

/** Underlined tab strip; `count` renders a small badge after the label. */
export function Tabs<T extends string>({ value, options, onChange, className }: {
  value: T;
  options: Array<{ value: T; label: ReactNode; count?: number | null; disabled?: boolean; title?: string }>;
  onChange: (v: T) => void;
  className?: string;
}) {
  return (
    <div role="tablist" className={cx("-mb-px flex min-w-0 gap-1 overflow-x-auto", className)}>
      {options.map((o) => (
        <button key={o.value} type="button" role="tab" aria-selected={value === o.value} disabled={o.disabled} title={o.title}
                onClick={() => onChange(o.value)}
                className={cx("flex items-center gap-1.5 whitespace-nowrap border-b-2 px-2.5 py-2 text-xs font-medium transition-colors disabled:opacity-40",
                  value === o.value ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg")}>
          {o.label}
          {o.count != null && <span className={cx("rounded-full px-1.5 text-[10px] tabular-nums", value === o.value ? "bg-accent/15 text-accent" : "bg-raised text-muted")}>{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

/** Loading placeholder that keeps the layout's shape. */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("sbx-skeleton rounded-md", className)} aria-hidden />;
}

/** Small uppercase section label. */
export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("text-[10.5px] font-semibold uppercase tracking-[0.08em] text-faint", className)}>{children}</div>;
}

/** Explicit "nothing here yet" state. Every panel uses it instead of blank space. */
export function Empty({ title, children, icon, className }: { title: string; children?: ReactNode; icon?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-line-strong/70 bg-panel/50 px-4 py-10 text-center", className)}>
      <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-accent/10 text-accent ring-1 ring-inset ring-accent/20">
        {icon ?? (
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.75" aria-hidden>
            <rect x="4" y="4" width="16" height="16" rx="3" />
            <path d="M8 12h8" strokeLinecap="round" />
          </svg>
        )}
      </span>
      <div className="text-sm font-medium text-fg">{title}</div>
      {children && <div className="max-w-md text-xs text-muted">{children}</div>}
    </div>
  );
}

export function ErrorNote({ error, onRetry, className }: { error: unknown; onRetry?: () => void; className?: string }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className={cx("flex items-start gap-2 rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad", className)}>
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
    <div className="rounded-lg border border-line bg-sunken/70 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-faint">{label}</div>
      <div className={cx("mt-0.5 font-mono text-base font-semibold tabular-nums", toneText)}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
    </div>
  );
}

export function Collapsible({ title, children, defaultOpen = false, right }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean; right?: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-panel">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-raised/70">
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
    <pre className={cx("overflow-auto whitespace-pre rounded-lg border border-line bg-sunken p-2.5 font-mono text-[11px] leading-relaxed text-fg/90", maxH, className)}>
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
    <div className="inline-flex rounded-lg bg-sunken p-0.5 ring-1 ring-line">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          onClick={() => onChange(o.value)}
          className={cx(
            "rounded-md px-2.5 font-medium transition-all",
            size === "sm" ? "h-6 text-[11px]" : "h-7 text-xs",
            value === o.value ? "bg-panel text-fg shadow-sm ring-1 ring-line" : "text-muted hover:text-fg",
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
        "h-8 rounded-xl border border-line bg-panel shadow-panel px-2 text-xs text-fg shadow-sm outline-none transition-colors hover:border-line-strong focus:border-accent disabled:opacity-50",
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
