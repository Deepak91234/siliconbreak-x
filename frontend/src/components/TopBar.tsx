/**
 * Environment status bar (GET /api/environment + environment/hardware events):
 * Docker, Vivado container, Verilator, openFPGALoader, board, LLM. Each chip
 * opens a popover with the raw details the backend reported.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { errorMessage } from "../api/client";
import type { Approval, Environment } from "../api/types";
import { useAppState, useStore } from "../store/store";
import { fmtAgo, fmtTime } from "../lib/format";
import { hardwareState, type Tone } from "../lib/status";
import { toast } from "../lib/toast";
import { useNow } from "../lib/useResource";
import { ApprovalCard } from "./ApprovalCard";
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

function Popover({ label, tone, pulse, children, wide }: { label: ReactNode; tone: Tone; pulse?: boolean; children: ReactNode; wide?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className={cx(
          "flex h-7 items-center gap-1.5 whitespace-nowrap rounded-md px-2 text-xs ring-1 ring-inset transition-colors",
          open ? "bg-raised ring-line" : "ring-transparent hover:bg-raised hover:ring-line",
        )}
      >
        <Dot tone={tone} pulse={pulse} />
        {label}
      </button>
      {open && (
        <div className={cx("absolute right-0 z-40 mt-1 rounded-lg border border-line bg-panel p-3 shadow-xl shadow-black/30", wide ? "w-[26rem]" : "w-80")}>
          {children}
        </div>
      )}
    </div>
  );
}

function VivadoChip({ env }: { env: Environment }) {
  const store = useStore();
  const v = env.vivado;
  const [busy, setBusy] = useState(false);
  const canStart = !v.ready && (v.can_start_headless || !v.docker_daemon);
  const managedRunning = v.containers?.some((c) => c.managed_by_circuitloop && c.running);
  const tone: Tone = v.ready ? (v.build_running ? "accent" : "ok") : v.docker_daemon ? "warn" : "bad";
  const label = v.ready
    ? `Vivado ${v.vivado_version ?? ""}`.trim()
    : "Vivado not ready";

  const start = async () => {
    setBusy(true);
    try {
      const a = await store.requestVivadoStart();
      toast(`Approval requested: ${a.title}`, "info");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  };
  const stop = async () => {
    setBusy(true);
    try {
      const r = await store.stopHeadlessVivado();
      toast(r.message || (r.ok ? "Stopped" : "Not stopped"), r.ok ? "ok" : "error");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-1">
      <Popover tone={tone} pulse={v.build_running} wide label={
        <span className="flex items-center gap-1.5">
          {label}
          {v.ready && v.active_container && <span className="hidden text-muted xl:inline">{v.active_container}</span>}
          {v.gui_session_running && <Pill tone="violet" className="!py-0">GUI</Pill>}
          {v.build_running && <Pill tone="accent" className="!py-0">building</Pill>}
        </span>
      }>
        <div className="space-y-3">
          <KV items={[
            ["Ready", v.ready ? "yes" : "no"],
            ["Active container", v.active_container ?? "none"],
            ["Vivado version", v.vivado_version ?? "not found"],
            ["Installed versions", v.vivado_versions?.join(", ") || "–"],
            ["GUI session", v.gui_session_running ? "running (left untouched)" : "not running"],
            ["hw_server", v.hw_server_running ? "running" : "not running"],
            ["Batch builds", String(v.batch_builds_running ?? 0)],
            ["Workspace shared", v.workspace_shared ? "yes" : "no"],
            ["Image present", v.image_present ? "yes" : "no"],
          ]} />
          {v.containers?.length > 0 && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">Containers</div>
              <ul className="space-y-1 text-xs">
                {v.containers.map((c) => (
                  <li key={c.name} className="flex items-center gap-2">
                    <Dot tone={c.running ? "ok" : c.exists ? "idle" : "muted"} />
                    <span className="font-mono">{c.name}</span>
                    <span className="text-muted">{c.running ? "running" : c.exists ? c.status ?? "stopped" : "absent"}</span>
                    {c.managed_by_circuitloop && <Pill tone="info" className="!py-0">CircuitLoop</Pill>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {v.problems?.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-4 text-xs text-warn">
              {v.problems.map((p, i) => <li key={i}>{p}</li>)}
            </ul>
          )}
          <div className="flex flex-wrap gap-2">
            {canStart && (
              <Button size="sm" variant="primary" busy={busy} onClick={() => void start()}>
                {v.docker_daemon ? "Start Vivado container" : "Start Docker"}
              </Button>
            )}
            {managedRunning && (
              <Button size="sm" variant="secondary" busy={busy} onClick={() => void stop()}>
                Stop headless container
              </Button>
            )}
          </div>
          <p className="text-[11px] text-faint">Starting a container always asks for your approval first.</p>
        </div>
      </Popover>
      {canStart && (
        <Button size="sm" variant="warn" busy={busy} onClick={() => void start()} title="Creates an approval request">
          {v.docker_daemon ? "Start Vivado container" : "Start Docker"}
        </Button>
      )}
    </div>
  );
}

function ApprovalsTray({ pending }: { pending: Approval[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className={cx(
          "flex h-7 items-center gap-1.5 rounded-md px-2 text-xs ring-1 ring-inset",
          pending.length ? "bg-warn/15 text-warn ring-warn/30" : "text-muted ring-transparent hover:bg-raised",
        )}
        title="Pending approvals"
      >
        <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
          <path d="M10 2.5l6.5 2.5v4.6c0 4-2.8 6.7-6.5 7.9-3.7-1.2-6.5-3.9-6.5-7.9V5z" strokeLinejoin="round" />
        </svg>
        {pending.length ? `${pending.length} pending` : "Approvals"}
      </button>
      {open && (
        <div className="absolute right-0 z-40 mt-1 max-h-[70vh] w-[24rem] space-y-2 overflow-auto rounded-lg border border-line bg-panel p-3 shadow-xl shadow-black/30">
          {pending.length === 0 ? (
            <div className="py-4 text-center text-xs text-muted">No approvals are waiting.</div>
          ) : (
            pending.map((a) => <ApprovalCard key={a.id} approval={a} />)
          )}
        </div>
      )}
    </div>
  );
}

export function TopBar({ theme, onToggleTheme }: { theme: "dark" | "light"; onToggleTheme: () => void }) {
  const store = useStore();
  const env = useAppState((s) => s.environment);
  const envError = useAppState((s) => s.environmentError);
  const hardware = useAppState((s) => s.hardware);
  const backend = useAppState((s) => s.backend);
  const socket = useAppState((s) => s.socket);
  const approvals = useAppState((s) => s.approvals);
  const [refreshing, setRefreshing] = useState(false);
  const now = useNow(15_000);

  const pending = useMemo(
    () => Object.values(approvals).filter((a) => a.status === "pending").sort((a, b) => b.created_at - a.created_at),
    [approvals],
  );

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

  const hw = hardware ?? env?.hardware ?? null;
  const hwInfo = hardwareState(hw?.state);
  const boardName = hw?.board?.name ?? null;
  const boardPart = hw?.board?.part ?? null;

  return (
    <header className="sticky top-0 z-30 flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-panel/95 px-3 py-1.5 backdrop-blur">
      <div className="flex items-center gap-2 pr-2">
        <svg viewBox="0 0 32 32" className="h-6 w-6" aria-hidden>
          <rect width="32" height="32" rx="7" className="fill-sunken" />
          <path d="M9 16a7 7 0 1 1 7 7" fill="none" stroke="rgb(var(--accent))" strokeWidth="3" strokeLinecap="round" />
          <circle cx="16" cy="23" r="2.5" fill="rgb(var(--ok))" />
        </svg>
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight">CircuitLoop</div>
          <div className="text-[10px] text-muted">
            {boardName || boardPart ? `${boardName ?? ""}${boardName && boardPart ? " · " : ""}${boardPart ?? ""}` : "FPGA engineering agent"}
          </div>
        </div>
      </div>

      <div className="order-last flex min-w-0 flex-1 basis-full flex-wrap items-center gap-0.5 md:order-none md:basis-0">
        <Popover
          tone={backend.reachable === false ? "bad" : socket.status === "open" ? "ok" : backend.reachable ? "warn" : "idle"}
          pulse={socket.status === "connecting"}
          label={backend.reachable === false ? "Backend offline" : socket.status === "open" ? "Live" : socket.status === "connecting" ? "Connecting…" : "Live updates off"}
        >
          <KV items={[
            ["Backend", backend.reachable === null ? "checking" : backend.reachable ? `reachable${backend.version ? ` (v${backend.version})` : ""}` : "unreachable"],
            ["Error", backend.error ?? "–"],
            ["Event stream", socket.status],
            ["Last event seq", String(socket.lastSeq)],
            ["Reconnect attempts", String(socket.attempts)],
            ["Socket error", socket.lastError ?? "–"],
          ]} />
        </Popover>

        {!env ? (
          <span className="px-2 text-xs text-muted">{envError ? `Environment unavailable: ${envError}` : "Checking environment…"}</span>
        ) : (
          <>
            <Popover
              tone={env.vivado.docker_daemon ? "ok" : env.vivado.docker_cli ? "bad" : "orange"}
              label={env.vivado.docker_daemon ? `Docker ${firstTokens(env.vivado.docker_server_version, 1)}` : env.vivado.docker_cli ? "Docker not running" : "Docker CLI missing"}
            >
              <KV items={[
                ["CLI", env.vivado.docker_cli ?? "not found"],
                ["Daemon", env.vivado.docker_daemon ? "running" : "not running"],
                ["Server version", env.vivado.docker_server_version ?? "–"],
                ["Error", env.vivado.docker_error ?? "–"],
              ]} />
            </Popover>
            <VivadoChip env={env} />
            <Popover
              tone={env.verilator.available ? "ok" : "bad"}
              label={env.verilator.available ? firstTokens(env.verilator.version) || "Verilator" : "Verilator missing"}
            >
              <KV items={[["Available", env.verilator.available ? "yes" : "no"], ["Path", env.verilator.path ?? "–"], ["Version", env.verilator.version ?? "–"]]} />
            </Popover>
            <Popover
              tone={env.openfpgaloader.available ? "ok" : "orange"}
              label={env.openfpgaloader.available ? `openFPGALoader ${versionToken(env.openfpgaloader.version)}` : "openFPGALoader missing"}
            >
              <KV items={[["Available", env.openfpgaloader.available ? "yes" : "no"], ["Path", env.openfpgaloader.path ?? "–"], ["Version", env.openfpgaloader.version ?? "–"]]} />
            </Popover>
            <Popover tone={hwInfo.tone} pulse={hw?.programming} label={hw ? (hw.state === "connected" ? "Board connected" : hw.state === "no_board" ? "Board not connected" : `Board: ${hwInfo.label}`) : "Board: unknown"}>
              {hw ? (
                <KV items={[
                  ["State", hwInfo.label],
                  ["Message", hw.message || "–"],
                  ["Preferred adapter", hw.preferred_adapter ?? "none"],
                  ["USB devices", String(hw.usb_devices?.length ?? 0)],
                  ["xvcd conflict", hw.xvc?.conflict ? "yes" : "no"],
                  ["Checked", fmtTime(hw.checked_at)],
                ]} />
              ) : (
                <div className="text-xs text-muted">No hardware status reported yet.</div>
              )}
              <button type="button" className="mt-2 text-[11px] text-accent hover:underline" onClick={() => store.setTab("hardware")}>
                Open Hardware tab
              </button>
            </Popover>
            <Popover tone={env.llm.configured ? "ok" : "warn"} label={env.llm.configured ? `${env.llm.provider} · ${env.llm.model}` : "LLM not configured – offline rules"}>
              <KV items={[
                ["Provider", env.llm.provider],
                ["Model", env.llm.model],
                ["Configured", env.llm.configured ? "yes" : "no (requests use the offline rule interpreter and preverified templates; automatic repair is unavailable)"],
              ]} />
            </Popover>
          </>
        )}
      </div>

      <div className="flex items-center gap-1">
        <ApprovalsTray pending={pending} />
        <Button variant="ghost" size="sm" busy={refreshing} onClick={() => void refresh()} title={env ? `Checked ${fmtAgo(env.checked_at, now)}` : "Refresh environment"}>
          <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
            <path d="M16 10a6 6 0 1 1-1.8-4.3M16 3.5v3.2h-3.2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="hidden sm:inline">{env ? fmtAgo(env.checked_at, now) : "Refresh"}</span>
        </Button>
        <Button variant="ghost" size="sm" onClick={onToggleTheme} title="Toggle light/dark theme">
          {theme === "dark" ? (
            <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="currentColor" aria-hidden><path d="M15.5 12.8A6.5 6.5 0 0 1 7.2 4.5a6.5 6.5 0 1 0 8.3 8.3z" /></svg>
          ) : (
            <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden><circle cx="10" cy="10" r="3.5" /><path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.3 4.3l1.4 1.4M14.3 14.3l1.4 1.4M4.3 15.7l1.4-1.4M14.3 5.7l1.4-1.4" strokeLinecap="round" /></svg>
          )}
        </Button>
      </div>
    </header>
  );
}
