/**
 * Top bar: the page title, one System status menu (GET /api/environment +
 * environment/hardware events: backend link, Docker, Vivado container,
 * Verilator, openFPGALoader, board, AI; each row opens the raw details the
 * backend reported and keeps its actions), approvals (pending + history),
 * presentation mode, Settings & usage, and theme.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { setUi } from "../lib/ui";
import { errorMessage } from "../api/client";
import type { Approval, Environment } from "../api/types";
import { useAppState, useStore } from "../store/store";
import { fmtAgo, fmtDateTime, fmtTime } from "../lib/format";
import { approvalStatus, hardwareState, type Tone } from "../lib/status";
import { toast } from "../lib/toast";
import { useNow } from "../lib/useResource";
import { ApprovalCard } from "./ApprovalCard";
import { IconChevronRight, IconCoins, IconMonitor, IconMoon, IconRefresh, IconShield, IconSun } from "./icons";
import { Button, Dot, KV, Pill, cx } from "./ui";

/** The version number in a tool's --version line ("openFPGALoader v1.1.1" -> "v1.1.1"). */
function versionToken(v: string | null | undefined): string {
  if (!v) return "";
  return v.match(/\bv?\d+(?:\.\d+)+\b/)?.[0] ?? firstTokens(v, 1);
}

function firstTokens(v: string | null | undefined, n = 2): string {
  if (!v) return "";
  return v.trim().split(/\s+/).slice(0, n).join(" ");
}

/** Click-outside / Escape dismissable dropdown anchored to its trigger. */
function useDropdown() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, ref };
}

interface StatusRow { key: string; label: string; value: string; tone: Tone; pulse?: boolean; details: ReactNode }

function VivadoActions({ env }: { env: Environment }) {
  const store = useStore();
  const v = env.vivado;
  const [busy, setBusy] = useState(false);
  const canStart = !v.ready && (v.can_start_headless || !v.docker_daemon);
  const managedRunning = v.containers?.some((c) => c.managed_by_circuitloop && c.running);
  const run = async (fn: () => Promise<unknown>, ok: (r: never) => string) => {
    setBusy(true);
    try {
      toast(ok((await fn()) as never), "info");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  };
  if (!canStart && !managedRunning) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {canStart && (
        <Button size="sm" variant="primary" busy={busy}
                onClick={() => void run(() => store.requestVivadoStart(), (a: { title: string }) => `Approval requested: ${a.title}`)}>
          {v.docker_daemon ? "Start Vivado container" : "Start Docker"}
        </Button>
      )}
      {managedRunning && (
        <Button size="sm" variant="secondary" busy={busy}
                onClick={() => void run(() => store.stopHeadlessVivado(), (r: { message?: string; ok: boolean }) => r.message || (r.ok ? "Stopped" : "Not stopped"))}>
          Stop headless container
        </Button>
      )}
      <p className="basis-full text-[11px] text-faint">Starting a container always asks for your approval first.</p>
    </div>
  );
}

function SystemStatus() {
  const store = useStore();
  const env = useAppState((s) => s.environment);
  const envError = useAppState((s) => s.environmentError);
  const hardware = useAppState((s) => s.hardware);
  const backend = useAppState((s) => s.backend);
  const socket = useAppState((s) => s.socket);
  const { open, setOpen, ref } = useDropdown();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const now = useNow(15_000);
  const hw = hardware ?? env?.hardware ?? null;
  const hwInfo = hardwareState(hw?.state);

  const rows: StatusRow[] = [{
    key: "link", label: "Backend & live updates",
    value: backend.reachable === false ? "backend offline" : socket.status === "open" ? "live" : socket.status === "connecting" ? "connecting…" : "live updates off",
    tone: backend.reachable === false ? "bad" : socket.status === "open" ? "ok" : backend.reachable ? "warn" : "idle", pulse: socket.status === "connecting",
    details: <KV items={[
      ["Backend", backend.reachable === null ? "checking" : backend.reachable ? `reachable${backend.version ? ` (v${backend.version})` : ""}` : "unreachable"],
      ["Error", backend.error ?? "–"], ["Event stream", socket.status], ["Last event seq", String(socket.lastSeq)],
      ["Reconnect attempts", String(socket.attempts)], ["Socket error", socket.lastError ?? "–"],
    ]} />,
  }];
  if (env) {
    const v = env.vivado;
    rows.push(
      { key: "docker", label: "Docker", value: v.docker_daemon ? firstTokens(v.docker_server_version, 1) || "running" : v.docker_cli ? "not running" : "CLI missing",
        tone: v.docker_daemon ? "ok" : v.docker_cli ? "bad" : "orange",
        details: <KV items={[["CLI", v.docker_cli ?? "not found"], ["Daemon", v.docker_daemon ? "running" : "not running"], ["Server version", v.docker_server_version ?? "–"], ["Error", v.docker_error ?? "–"]]} /> },
      { key: "vivado", label: "Vivado", value: v.ready ? `${v.vivado_version ?? ""} · ${v.active_container ?? ""}${v.build_running ? " · building" : ""}` : "not ready",
        tone: v.ready ? (v.build_running ? "accent" : "ok") : v.docker_daemon ? "warn" : "bad", pulse: v.build_running,
        details: (
          <div className="space-y-2">
            <KV items={[
              ["Ready", v.ready ? "yes" : "no"], ["Active container", v.active_container ?? "none"], ["Vivado version", v.vivado_version ?? "not found"],
              ["Installed versions", v.vivado_versions?.join(", ") || "–"], ["GUI session", v.gui_session_running ? "running (left untouched)" : "not running"],
              ["hw_server", v.hw_server_running ? "running" : "not running"], ["Batch builds", String(v.batch_builds_running ?? 0)],
              ["Workspace shared", v.workspace_shared ? "yes" : "no"], ["Image present", v.image_present ? "yes" : "no"],
            ]} />
            {v.containers?.length > 0 && (
              <ul className="space-y-1 text-xs">
                {v.containers.map((c) => (
                  <li key={c.name} className="flex items-center gap-2">
                    <Dot tone={c.running ? "ok" : c.exists ? "idle" : "muted"} />
                    <span className="font-mono">{c.name}</span>
                    <span className="text-muted">{c.running ? "running" : c.exists ? c.status ?? "stopped" : "absent"}</span>
                    {c.managed_by_circuitloop && <Pill tone="info" className="!py-0">managed</Pill>}
                  </li>
                ))}
              </ul>
            )}
            {v.problems?.length > 0 && <ul className="list-disc space-y-0.5 pl-4 text-xs text-warn">{v.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}
            <VivadoActions env={env} />
          </div>
        ) },
      { key: "verilator", label: "Verilator", value: env.verilator.available ? versionToken(env.verilator.version) || "available" : "missing", tone: env.verilator.available ? "ok" : "bad",
        details: <KV items={[["Available", env.verilator.available ? "yes" : "no"], ["Path", env.verilator.path ?? "–"], ["Version", env.verilator.version ?? "–"]]} /> },
      { key: "ofl", label: "openFPGALoader", value: env.openfpgaloader.available ? versionToken(env.openfpgaloader.version) : "missing", tone: env.openfpgaloader.available ? "ok" : "orange",
        details: <KV items={[["Available", env.openfpgaloader.available ? "yes" : "no"], ["Path", env.openfpgaloader.path ?? "–"], ["Version", env.openfpgaloader.version ?? "–"]]} /> },
    );
  }
  rows.push({
    key: "board", label: "Basys 3", value: hw ? hwInfo.label : "unknown", tone: hw ? hwInfo.tone : "idle", pulse: hw?.programming,
    details: (
      <div>
        {hw ? <KV items={[["State", hwInfo.label], ["Message", hw.message || "–"], ["Preferred adapter", hw.preferred_adapter ?? "none"],
          ["USB devices", String(hw.usb_devices?.length ?? 0)], ["xvcd conflict", hw.xvc?.conflict ? "yes" : "no"], ["Checked", fmtTime(hw.checked_at)]]} />
          : <div className="text-xs text-muted">No hardware status reported yet.</div>}
        <button type="button" className="mt-2 text-[11px] text-accent hover:underline" onClick={() => { setOpen(false); store.setTab("hardware"); }}>
          Detect and connection details (FPGA Lab)
        </button>
      </div>
    ),
  });
  if (env) {
    rows.push({
      key: "ai", label: "AI", value: env.llm.configured ? env.llm.model : env.llm.key_present ? "live AI off" : "offline rules", tone: env.llm.configured ? "ok" : "warn",
      details: (
        <div>
          <KV items={[
            ["Mode", env.llm.configured ? "model-powered agent" : "rules-based offline fallback"], ["Provider", env.llm.provider], ["Model", env.llm.model],
            ["Configured", env.llm.configured ? "yes" : env.llm.key_present ? "key present, live AI calls off (turn them on in Settings & usage)"
              : "no (offline rule interpreter and preverified templates; automatic repair unavailable)"],
          ]} />
          <button type="button" className="mt-2 text-[11px] text-accent hover:underline" onClick={() => { setOpen(false); setUi({ settings: true }); }}>
            Settings & usage
          </button>
        </div>
      ),
    });
  }

  const issues = rows.filter((r) => r.tone === "bad" || r.tone === "warn" || r.tone === "orange").length;
  const worst: Tone = rows.some((r) => r.tone === "bad") ? "bad" : issues ? "warn" : rows.some((r) => r.tone === "accent") ? "accent" : "ok";
  const refresh = async () => {
    setRefreshing(true);
    try {
      await store.loadEnvironment(true);
    } catch (e) {
      toast(`Environment refresh failed: ${errorMessage(e)}`, "error");
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
              className={cx("flex h-8 items-center gap-2 rounded-full px-3 text-xs font-medium ring-1 ring-inset transition-colors",
                open ? "bg-raised text-fg ring-line-strong" : "bg-raised/40 text-muted ring-line/70 hover:bg-raised hover:text-fg")}>
        <Dot tone={worst} pulse={rows.some((r) => r.pulse)} />
        System status
        <span className="text-faint">{!env ? (envError ? "unavailable" : "checking…") : issues ? `${issues} need${issues === 1 ? "s" : ""} attention` : "all ready"}</span>
      </button>
      {open && (
        <div className="sbx-enter absolute right-0 z-40 mt-1.5 w-[24rem] max-w-[92vw] rounded-xl border border-line bg-panel p-2 shadow-pop">
          {envError && <p className="px-2 py-1 text-xs text-bad">Environment unavailable: {envError}</p>}
          <ul className="divide-y divide-line">
            {rows.map((r) => (
              <li key={r.key}>
                <button type="button" onClick={() => setExpanded(expanded === r.key ? null : r.key)}
                        className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-xs hover:bg-raised">
                  <Dot tone={r.tone} pulse={r.pulse} />
                  <span className="font-medium text-fg">{r.label}</span>
                  <span className="ml-auto truncate pl-2 text-muted">{r.value}</span>
                  <IconChevronRight size={13} className={cx("shrink-0 text-faint transition-transform", expanded === r.key && "rotate-90")} />
                </button>
                {expanded === r.key && <div className="px-3 pb-3 pt-1">{r.details}</div>}
              </li>
            ))}
          </ul>
          <div className="mt-1 flex items-center justify-between border-t border-line px-2 pt-2 text-[11px] text-faint">
            <span>{env ? `checked ${fmtAgo(env.checked_at, now)}` : ""}</span>
            <Button size="sm" variant="ghost" busy={refreshing} onClick={() => void refresh()}><IconRefresh size={13} /> Refresh</Button>
          </div>
        </div>
      )}
    </div>
  );
}

function ApprovalsTray() {
  const approvals = useAppState((s) => s.approvals);
  const { open, setOpen, ref } = useDropdown();
  const [tab, setTab] = useState<"pending" | "history">("pending");
  const pending = useMemo(() => Object.values(approvals).filter((a) => a.status === "pending").sort((a, b) => b.created_at - a.created_at), [approvals]);
  const history = useMemo(() => Object.values(approvals).filter((a) => a.status !== "pending").sort((a, b) => b.created_at - a.created_at).slice(0, 50), [approvals]);
  useEffect(() => { if (pending.length) setTab("pending"); }, [pending.length]);
  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={() => setOpen(!open)} title="Approvals"
              className={cx("flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium ring-1 ring-inset transition-colors",
                pending.length ? "bg-warn/15 text-warn ring-warn/40 shadow-[0_0_18px_-6px_rgb(var(--warn)/0.7)]" : "text-muted ring-transparent hover:bg-raised hover:text-fg")}>
        <IconShield size={14} />
        {pending.length ? `${pending.length} pending` : "Approvals"}
      </button>
      {open && (
        <div className="sbx-enter absolute right-0 z-40 mt-1.5 max-h-[70vh] w-[26rem] max-w-[92vw] overflow-auto rounded-xl border border-line bg-panel p-3 shadow-pop">
          <div className="mb-2 flex gap-1">
            {(["pending", "history"] as const).map((t) => (
              <button key={t} type="button" onClick={() => setTab(t)}
                      className={cx("rounded-lg px-2.5 py-1 text-xs font-medium", tab === t ? "bg-raised text-fg" : "text-muted hover:text-fg")}>
                {t === "pending" ? `Pending (${pending.length})` : `History (${history.length})`}
              </button>
            ))}
          </div>
          {tab === "pending" && (pending.length === 0
            ? <div className="py-4 text-center text-xs text-muted">No approvals are waiting.</div>
            : <div className="space-y-2">{pending.map((a) => <ApprovalCard key={a.id} approval={a} />)}</div>)}
          {tab === "history" && (history.length === 0
            ? <div className="py-4 text-center text-xs text-muted">No decided approvals yet.</div>
            : <ul className="divide-y divide-line">{history.map((a) => <HistoryRow key={a.id} a={a} />)}</ul>)}
        </div>
      )}
    </div>
  );
}

function HistoryRow({ a }: { a: Approval }) {
  const [open, setOpen] = useState(false);
  const st = approvalStatus(a.status);
  return (
    <li className="py-1.5">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 text-left text-xs">
        <Pill tone={st.tone}>{st.label}</Pill>
        <span className="min-w-0 flex-1 truncate">{a.title}</span>
        <span className="shrink-0 text-[11px] text-faint">{fmtDateTime(a.decided_at ?? a.created_at)}</span>
      </button>
      {open && <div className="mt-2"><ApprovalCard approval={a} /></div>}
    </li>
  );
}

export function TopBar({ theme, onToggleTheme, title }: { theme: "dark" | "light"; onToggleTheme: () => void; title: ReactNode }) {
  return (
    <header className="sticky top-0 z-30 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-panel/85 px-4 py-2 backdrop-blur-md">
      <div className="min-w-0 pr-1">{title}</div>
      <div className="ml-auto flex items-center gap-1.5">
        <SystemStatus />
        <ApprovalsTray />
        <Button variant="ghost" size="sm" onClick={() => setUi({ settings: true })} title="Settings & usage (AI providers, credits, live AI)">
          <IconCoins size={15} /><span className="hidden lg:inline">Settings & usage</span>
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setUi({ presentation: true })} title="Presentation mode for demos">
          <IconMonitor size={15} />
        </Button>
        <Button variant="ghost" size="sm" onClick={onToggleTheme} title="Toggle light/dark theme">
          {theme === "dark" ? <IconMoon size={15} /> : <IconSun size={15} />}
        </Button>
      </div>
    </header>
  );
}
