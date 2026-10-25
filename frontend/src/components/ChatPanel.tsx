/**
 * Conversation with the agent for the selected project: history from
 * GET /api/projects/{id}, live agent.message events, inline approval cards,
 * and the composer (new design -> POST /api/projects, follow-up ->
 * POST /api/projects/{id}/messages) with the run options.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { errorMessage } from "../api/client";
import type { Approval, Message, Run, RunOptions } from "../api/types";
import { isRunActive, type ProjectDetail } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { fmtTime, prettyJson } from "../lib/format";
import { runStatus } from "../lib/status";
import { toast } from "../lib/toast";
import { ApprovalCard } from "./ApprovalCard";
import { StatusIcon } from "./StatusIcon";
import { Button, Collapsible, Pill, Pre, Select, Spinner, cx } from "./ui";
import { useRevision } from "../tabs/VerilogTab";
import { faultsForFamily } from "../lib/faults";

function SpecSummary({ spec }: { spec: Record<string, unknown> }) {
  const behavior = (spec.behavior ?? {}) as Record<string, unknown>;
  const outputs = (spec.outputs ?? []) as Array<{ name?: string; indices?: number[] }>;
  const inputs = (spec.inputs ?? []) as Array<{ name?: string; port?: string; index?: number | null }>;
  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex flex-wrap gap-1">
        {typeof behavior.family === "string" && <Pill tone="accent">{behavior.family}</Pill>}
        {outputs.map((o, i) => (
          <Pill key={`o${i}`} tone="ok">{o.name ?? "out"}: LED{o.indices?.length ? `${Math.min(...o.indices)}-${Math.max(...o.indices)}` : "?"}</Pill>
        ))}
        {inputs.map((x, i) => (
          <Pill key={`i${i}`} tone="info">{x.name ?? "in"}: {x.port}{x.index != null ? `[${x.index}]` : ""}</Pill>
        ))}
      </div>
      <Collapsible title="Specification (DesignSpec JSON)">
        <Pre maxH="max-h-72">{prettyJson(spec)}</Pre>
      </Collapsible>
    </div>
  );
}

function MessageBubble({ m, approvals, onRun }: { m: Message; approvals: Record<string, Approval>; onRun: (id: string) => void }) {
  const isUser = m.role === "user";
  const isSystem = m.role === "system";
  const approvalId = typeof m.data?.approval_id === "string" ? (m.data.approval_id as string) : null;
  const approval = approvalId ? approvals[approvalId] : undefined;
  const spec = m.kind === "spec" && m.data?.spec && typeof m.data.spec === "object" ? (m.data.spec as Record<string, unknown>) : null;

  if (m.kind === "approval" && approval) {
    return (
      <div className="pr-6">
        <ApprovalCard approval={approval} />
      </div>
    );
  }
  if (isSystem) {
    return <div className="px-6 text-center text-[11px] text-muted">{m.text}</div>;
  }
  return (
    <div className={cx("flex", isUser ? "justify-end pl-8" : "justify-start pr-6")}>
      <div
        className={cx(
          "min-w-0 max-w-full rounded-xl px-3 py-2 text-[13px] leading-relaxed",
          isUser && "rounded-br-sm bg-accent/15 text-fg ring-1 ring-accent/25",
          !isUser && m.kind === "error" && "rounded-bl-sm border border-bad/40 bg-bad/10",
          !isUser && m.kind === "result" && "rounded-bl-sm border border-ok/30 bg-ok/10",
          !isUser && m.kind === "approval" && "rounded-bl-sm border border-warn/40 bg-warn/10",
          !isUser && (m.kind === "text" || m.kind === "spec") && "rounded-bl-sm border border-line bg-raised/70",
        )}
      >
        {!isUser && (
          <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
            <span>CircuitLoop agent</span>
            {m.kind !== "text" && <span className="text-faint">{m.kind}</span>}
          </div>
        )}
        <div className="whitespace-pre-wrap break-words">{m.text}</div>
        {spec && <SpecSummary spec={spec} />}
        {m.kind === "approval" && approvalId && !approval && (
          <div className="mt-1 text-[11px] text-muted">Approval {approvalId} is no longer pending.</div>
        )}
        <div className="mt-1 flex items-center gap-2 text-[10px] text-faint">
          <span>{fmtTime(m.created_at)}</span>
          {m.run_id && (
            <button type="button" className="font-mono hover:text-accent" onClick={() => onRun(m.run_id!)}>
              {m.run_id}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** What the agent is doing right now, straight from the run's stage list. */
function WorkingIndicator({ run }: { run: Run }) {
  const active = run.stages.find((s) => s.status === "running") ?? run.stages.find((s) => s.status === "blocked");
  return (
    <div className="flex items-center gap-2 rounded-lg border border-accent/30 bg-accent/5 px-3 py-2 text-xs">
      {active ? <StatusIcon status={active.status} className="h-3.5 w-3.5" /> : <Spinner className="h-3.5 w-3.5 text-accent" />}
      <span className="font-medium">{active ? active.label : runStatus(run.status).label}</span>
      {active?.summary && <span className="min-w-0 flex-1 truncate text-muted">{active.summary}</span>}
    </div>
  );
}

function RunOptionsBar({ options, onChange, disabled, family }: {
  options: RunOptions;
  onChange: (o: RunOptions) => void;
  disabled: boolean;
  /** behavior.family of the revision a follow-up would modify; null for a new design (not interpreted yet). */
  family: string | null;
}) {
  const allFaults = useAppState((s) => s.faults);
  const faultsError = useAppState((s) => s.faultsError);
  const faults = useMemo(() => faultsForFamily(allFaults, family), [allFaults, family]);
  const selected = faults?.find((f) => f.name === options.inject_fault);
  // Never keep a fault armed that does not apply to the current design family.
  useEffect(() => {
    if (options.inject_fault && faults && !faults.some((f) => f.name === options.inject_fault)) onChange({ ...options, inject_fault: null });
  }, [faults, options, onChange]);
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
        <label className="flex cursor-pointer items-center gap-1.5 text-muted hover:text-fg">
          <input
            type="checkbox"
            className="accent-[rgb(var(--accent))]"
            checked={options.simulation_only}
            disabled={disabled}
            onChange={(e) => onChange({ ...options, simulation_only: e.target.checked })}
          />
          Simulation only <span className="text-faint">(skip Vivado and hardware)</span>
        </label>
        <label className="flex items-center gap-1.5 text-muted" title="Deliberately inserts a known defect into cl_core so you can watch simulation catch it and the agent repair it.">
          <span className={cx(options.inject_fault && "font-semibold text-warn")}>Fault injection test mode</span>
          <Select
            value={options.inject_fault ?? ""}
            onChange={(v) => onChange({ ...options, inject_fault: v || null })}
            disabled={disabled || !faults || faults.length === 0}
            className={cx("max-w-[12rem]", options.inject_fault && "border-warn text-warn")}
            title={faultsError ?? undefined}
          >
            <option value="">off</option>
            {faults?.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name}{f.families?.length ? ` (${f.families.join(", ")})` : ""}
              </option>
            ))}
          </Select>
          {!faults && !faultsError && <span className="text-faint">loading…</span>}
          {faultsError && <span className="text-faint">unavailable</span>}
          {faults && faults.length === 0 && !faultsError && (
            <span className="text-faint">{family ? `none for ${family}` : "none offered"}</span>
          )}
        </label>
      </div>
      {faults && faults.length > 0 && !options.inject_fault && (
        <p className="text-[11px] text-faint">
          Test mode deliberately injects a known defect to demonstrate automatic detection and repair.{" "}
          {family
            ? `Showing faults for the current design family (${family}).`
            : "The design family is not known until the request is interpreted, so all faults are listed; one that does not fit the interpreted design cannot be applied."}
        </p>
      )}
      {options.inject_fault && (
        <div className="rounded-md border border-warn/40 bg-warn/10 px-2 py-1.5 text-[11px] text-warn">
          <span className="font-semibold">TEST MODE:</span> the next run deliberately injects the defect
          <span className="font-mono"> {options.inject_fault}</span> into the generated cl_core to demonstrate detection and automatic repair.
          {selected?.description ? ` ${selected.description}` : ""}
        </div>
      )}
    </div>
  );
}

function Composer({ projectId, disabled }: { projectId: string | null; disabled: boolean }) {
  const store = useStore();
  const [text, setText] = useState("");
  const [options, setOptions] = useState<RunOptions>({ inject_fault: null, simulation_only: false });
  const [sending, setSending] = useState(false);
  // A follow-up modifies the project's current revision: its spec names the design family.
  const currentRevisionId = useAppState((s) => {
    const det = projectId ? s.details[projectId] : undefined;
    return det ? det.project.current_revision_id ?? det.revisions[det.revisions.length - 1]?.id ?? null : null;
  });
  const currentRev = useRevision(currentRevisionId);
  const family = typeof currentRev.data?.spec?.behavior?.family === "string" ? currentRev.data.spec.behavior.family : null;
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const onFill = (e: Event) => {
      const t = (e as CustomEvent<string>).detail;
      setText(t);
      ref.current?.focus();
    };
    window.addEventListener("circuitloop:fill-composer", onFill);
    return () => window.removeEventListener("circuitloop:fill-composer", onFill);
  }, []);

  const send = async () => {
    const t = text.trim();
    if (!t || sending) return;
    setSending(true);
    setError(null);
    try {
      if (projectId) await store.postMessage(projectId, t, options);
      else await store.createProject(t, options);
      setText("");
      // A fault applies to one run only; never leave the test mode armed by accident.
      if (options.inject_fault) setOptions({ ...options, inject_fault: null });
    } catch (e) {
      setError(errorMessage(e));
      toast(`Request not sent: ${errorMessage(e)}`, "error");
    } finally {
      setSending(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="space-y-2 border-t border-line bg-panel p-3">
      {error && <div className="rounded border border-bad/30 bg-bad/10 px-2 py-1 text-[11px] text-bad">{error}</div>}
      <div className="rounded-lg border border-line bg-sunken focus-within:border-accent">
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          rows={3}
          maxLength={4000}
          disabled={disabled}
          placeholder={projectId ? "Ask for a change to this design… (Enter to send, Shift+Enter for a new line)" : "Describe the circuit you want on the Basys 3…"}
          className="block w-full resize-none bg-transparent px-3 py-2 text-[13px] outline-none placeholder:text-faint disabled:opacity-60"
        />
        <div className="flex items-center justify-between gap-2 px-2 pb-2">
          <span className="text-[10px] text-faint">{projectId ? "Follow-up modifies the current revision" : "Starts a new project"}</span>
          <Button variant="primary" size="sm" busy={sending} disabled={disabled || !text.trim()} onClick={() => void send()}>
            {projectId ? "Send" : "Create design"}
          </Button>
        </div>
      </div>
      <details className="group rounded-md border border-line/70 px-2 py-1" open={!!options.inject_fault || options.simulation_only}>
        <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[11px] text-faint hover:text-fg">
          <span className="transition-transform group-open:rotate-90">›</span>
          Advanced / developer tools
          {(options.inject_fault || options.simulation_only) && (
            <span className="ml-auto rounded bg-warn/15 px-1.5 text-[10px] font-semibold text-warn">
              {options.inject_fault ? "fault armed" : "simulation only"}
            </span>
          )}
        </summary>
        <div className="pb-1 pt-2">
          <RunOptionsBar options={options} onChange={setOptions} disabled={disabled || sending} family={family} />
        </div>
      </details>
    </div>
  );
}

function Welcome() {
  const llm = useAppState((st) => st.environment?.llm ?? null);
  return (
    <div className="space-y-3 px-1 py-6">
      <h2 className="text-base font-semibold">What should the Basys 3 do?</h2>
      <p className="text-xs leading-relaxed text-muted">
        Describe LED behaviour driven by the clock, buttons and switches: counters, blinkers, LED patterns or a
        traffic light. Pick an example in the workspace or write your own below.
      </p>
      {llm && (
        <p className="text-[11px] text-faint">
          {llm.configured ? `Agent: ${llm.provider} ${llm.model}.` : llm.key_present
            ? "Agent: offline rules mode (live AI calls are off; turn them on in the Security tab's AI credits panel)."
            : "Agent: offline rules mode (no API key). Supported phrasings and preverified templates only."}
        </p>
      )}
    </div>
  );
}

function ProjectPicker({ projectId }: { projectId: string | null }) {
  const store = useStore();
  const projects = useAppState((s) => s.projects);
  const projectsError = useAppState((s) => s.projectsError);
  return (
    <div className="flex items-center gap-2 border-b border-line px-3 py-2">
      <Select
        value={projectId ?? ""}
        onChange={(v) => store.selectProject(v || null)}
        className="min-w-0 flex-1"
        title={projectsError ?? undefined}
      >
        <option value="">{projects === null ? (projectsError ? "Projects unavailable" : "Loading projects…") : "— New design —"}</option>
        {projects?.map((p) => (
          <option key={p.id} value={p.id}>
            {p.title} ({p.id})
          </option>
        ))}
      </Select>
      <Button size="sm" variant={projectId ? "secondary" : "ghost"} onClick={() => store.selectProject(null)} title="Start a new design">
        + New
      </Button>
      <Button size="sm" variant="ghost" title="New security audit (SiliconBreak X)"
              onClick={() => { store.selectProject(null); store.setTab("security"); }}>
        Audit
      </Button>
    </div>
  );
}

export function ChatPanel() {
  const store = useStore();
  const selection = useAppState((s) => s.selection);
  const projectId = selection.projectId;
  const detail: ProjectDetail | undefined = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const projectError = useAppState((s) => (projectId ? s.projectErrors[projectId] : undefined));
  const approvals = useAppState((s) => s.approvals);
  const runs = useAppState((s) => s.runs);
  const reachable = useAppState((s) => s.backend.reachable);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const messages = detail?.messages ?? [];
  const latestRun = detail?.project.latest_run_id ? runs[detail.project.latest_run_id] : undefined;

  const referenced = useMemo(() => {
    const ids = new Set<string>();
    for (const m of messages) if (typeof m.data?.approval_id === "string") ids.add(m.data.approval_id as string);
    return ids;
  }, [messages]);

  // Pending approvals for this project (or global ones, e.g. starting Vivado) not already shown inline.
  const loosePending = useMemo(
    () =>
      Object.values(approvals)
        .filter((a) => a.status === "pending" && !referenced.has(a.id) && (a.project_id === projectId || a.project_id === null))
        .sort((a, b) => a.created_at - b.created_at),
    [approvals, referenced, projectId],
  );

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages.length, loosePending.length, latestRun?.status, projectId]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const st = latestRun ? runStatus(latestRun.status) : null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <ProjectPicker projectId={projectId} />
      {detail && (
        <div className="flex items-center gap-2 border-b border-line px-3 py-1.5 text-[11px] text-muted">
          <span className="min-w-0 flex-1 truncate font-medium text-fg">{detail.project.title}</span>
          <span>{detail.project.revision_count} rev</span>
          {st && <Pill tone={st.tone}>{st.label}</Pill>}
        </div>
      )}
      <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
        {!projectId && <Welcome />}
        {projectId && !detail && !projectError && <div className="py-6 text-center text-xs text-muted"><Spinner className="mx-auto mb-2 h-4 w-4" />Loading conversation…</div>}
        {projectId && projectError && !detail && (
          <div className="rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">
            Could not load project {projectId}: {projectError}{" "}
            <button type="button" className="underline" onClick={() => store.selectProject(projectId)}>retry</button>
          </div>
        )}
        {detail && messages.length === 0 && <div className="py-6 text-center text-xs text-muted">No messages in this project yet.</div>}
        {messages.map((m) => (
          <MessageBubble key={m.id} m={m} approvals={approvals} onRun={(id) => store.selectRun(id)} />
        ))}
        {loosePending.map((a) => (
          <div key={a.id} className="pr-6">
            <ApprovalCard approval={a} />
          </div>
        ))}
        {latestRun && isRunActive(latestRun) && <WorkingIndicator run={latestRun} />}
      </div>
      <Composer projectId={projectId} disabled={reachable === false} />
    </div>
  );
}
