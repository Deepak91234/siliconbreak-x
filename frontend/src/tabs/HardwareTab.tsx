/**
 * Board and programmer status (GET /api/hardware, hardware.updated events):
 * USB devices, the macOS xvcd conflict, adapters, JTAG detect, and programming.
 * Programming and stopping xvcd only *request* approvals; the last programming
 * result is shown exactly as the backend reported it.
 */

import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../api/client";
import type { Approval, HardwareStatus, Run } from "../api/types";
import { isRunActive } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { fmtDateTime, fmtDuration, fmtTime, hex4, shortSha } from "../lib/format";
import { hardwareState } from "../lib/status";
import { toast } from "../lib/toast";
import { ApprovalCard } from "../components/ApprovalCard";
import { Button, Card, Collapsible, CopyButton, Empty, KV, Loading, Pill, Pre, cx } from "../components/ui";
import { StatusIcon } from "../components/StatusIcon";
import { useRevision } from "./VerilogTab";

function Adapters({ hw }: { hw: HardwareStatus }) {
  const entries = Object.entries(hw.adapters ?? {});
  if (!entries.length) return <p className="text-xs text-muted">No adapters reported.</p>;
  return (
    <ul className="space-y-1.5">
      {entries.map(([name, a]) => (
        <li key={name} className="flex flex-wrap items-center gap-2 text-xs">
          <StatusIcon status={a.available ? "passed" : "unavailable"} className="h-3.5 w-3.5" />
          <span className="font-mono font-medium">{name}</span>
          {hw.preferred_adapter === name && <Pill tone="accent">preferred</Pill>}
          <span className="text-muted">{a.available ? [a.version, a.path].filter(Boolean).join(" · ") : a.reason ?? "not available"}</span>
        </li>
      ))}
    </ul>
  );
}

function XvcPanel({ hw }: { hw: HardwareStatus }) {
  const store = useStore();
  const [busy, setBusy] = useState<number | null>(null);
  const x = hw.xvc;
  if (!x) return <p className="text-xs text-muted">No xvcd information.</p>;
  const stop = async (pid: number) => {
    setBusy(pid);
    try {
      const a = await store.requestStopXvcd(pid);
      toast(`Approval requested: ${a.title}`, "info");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center gap-2">
        <Pill tone={x.conflict ? "warn" : "ok"}>{x.conflict ? "conflict" : "no conflict"}</Pill>
        <span className="text-muted">xvcd launcher {x.launcher_running ? "running" : "not running"}</span>
      </div>
      {x.note && <p className={cx("rounded border px-2 py-1.5", x.conflict ? "border-warn/30 bg-warn/10 text-warn" : "border-line text-muted")}>{x.note}</p>}
      {x.xvcd_pids?.length > 0 && (
        <ul className="space-y-1">
          {x.xvcd_pids.map((pid) => (
            <li key={pid} className="flex items-center gap-2">
              <span className="font-mono">xvcd pid {pid}</span>
              <Button size="sm" variant="warn" busy={busy === pid} onClick={() => void stop(pid)} title="Creates an approval; nothing is stopped until you approve">
                Stop xvcd (needs approval)
              </Button>
            </li>
          ))}
        </ul>
      )}
      {x.port_2542_listeners?.length > 0 && (
        <Collapsible title={`Port 2542 listeners (${x.port_2542_listeners.length})`}>
          <Pre maxH="max-h-40">{JSON.stringify(x.port_2542_listeners, null, 2)}</Pre>
        </Collapsible>
      )}
    </div>
  );
}

function LastProgram({ hw }: { hw: HardwareStatus }) {
  const p = hw.last_program;
  if (!p) return <p className="text-xs text-muted">The board has not been programmed by CircuitLoop since the backend started.</p>;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs">
        <StatusIcon status={p.ok ? "passed" : "failed"} className="h-4 w-4" />
        <span className="font-semibold">{p.state}</span>
        <span className="text-muted">{p.message}</span>
      </div>
      <KV items={[
        ["Adapter", p.adapter],
        ["ok", String(p.ok)],
        ["Bitstream", <span key="b" className="font-mono">{p.bitstream_path}</span>],
        ["SHA-256", <span key="s" className="flex items-center gap-2 font-mono" title={p.bitstream_sha256}>{shortSha(p.bitstream_sha256, 24)} <CopyButton text={p.bitstream_sha256} /></span>],
        ["Duration", fmtDuration(p.duration_s)],
        ["Finished", fmtDateTime(p.finished_at)],
      ]} />
      {p.raw && (
        <Collapsible title="Programmer output (raw)">
          <Pre maxH="max-h-64">{p.raw}</Pre>
        </Collapsible>
      )}
    </div>
  );
}

export function HardwareTab({ revisionId, run }: { revisionId: string | null; run: Run | null }) {
  const store = useStore();
  const hw = useAppState((s) => s.hardware);
  const approvals = useAppState((s) => s.approvals);
  const rev = useRevision(revisionId);
  const [detecting, setDetecting] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    void store.loadHardware().finally(() => setLoaded(true));
  }, [store]);

  const programApprovals = useMemo(
    () =>
      Object.values(approvals)
        .filter((a: Approval) => a.kind === "program_fpga" && (a.status === "pending" || (revisionId && a.details?.revision_id === revisionId)))
        .sort((a, b) => b.created_at - a.created_at)
        .slice(0, 3),
    [approvals, revisionId],
  );

  const detect = async () => {
    setDetecting(true);
    try {
      const h = await store.detectHardware();
      toast(`Detect: ${h.last_detect?.message ?? hardwareState(h.state).label}`, h.last_detect?.ok ? "ok" : "info");
    } catch (e) {
      toast(`Detect failed: ${errorMessage(e)}`, "error");
    } finally {
      setDetecting(false);
    }
  };

  const program = async () => {
    if (!revisionId) return;
    setRequesting(true);
    try {
      const a = await store.requestProgram(revisionId);
      toast(`Approval requested: ${a.title}`, "info");
    } catch (e) {
      toast(`Cannot program: ${errorMessage(e)}`, "error");
    } finally {
      setRequesting(false);
    }
  };

  if (!hw && !loaded) return <Loading label="Reading hardware status…" />;
  if (!hw) return <Empty title="Hardware status unavailable">The backend did not report hardware status. Check that it is running.</Empty>;

  const st = hardwareState(hw.state);
  const r = rev.data;
  const hasBitstream = !!r?.build?.bitstream_sha256 && r.build_status === "passed";
  const pendingProgram = programApprovals.some((a) => a.status === "pending");
  const programStages = run?.stages.filter((s) => s.key === "detect" || s.key === "program") ?? [];

  return (
    <div className="space-y-3">
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card title="Board" actions={<Button size="sm" variant="secondary" busy={detecting} onClick={() => void detect()}>Detect</Button>}>
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className={cx("inline-flex h-9 w-9 items-center justify-center rounded-lg ring-1", hw.state === "connected" ? "bg-ok/15 ring-ok/30" : "bg-raised ring-line")}>
                <svg viewBox="0 0 24 24" className={cx("h-5 w-5", hw.state === "connected" ? "text-ok" : "text-muted")} fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
                  <rect x="4" y="4" width="16" height="16" rx="2" /><rect x="8.5" y="8.5" width="7" height="7" rx="1" /><path d="M9 1.5v2.5M15 1.5v2.5M9 20v2.5M15 20v2.5M1.5 9H4M1.5 15H4M20 9h2.5M20 15h2.5" strokeLinecap="round" />
                </svg>
              </span>
              <div>
                <div className="flex items-center gap-2 text-sm font-semibold">
                  {hw.state === "connected" ? "Board connected" : hw.state === "no_board" ? "Board not connected" : `Board ${st.label}`}
                  <Pill tone={st.tone}>{st.label}</Pill>
                  {hw.programming && <Pill tone="accent">programming…</Pill>}
                </div>
                <div className="text-xs text-muted">{hw.message}</div>
              </div>
            </div>
            <KV items={[
              ["Board", hw.board?.name ?? "–"],
              ["Part", hw.board?.part ?? "–"],
              ["Expected IDCODE", hw.board?.idcode ?? "–"],
              ["Status checked", fmtTime(hw.checked_at)],
            ]} />
            {hw.last_detect && (
              <div className="rounded-md border border-line p-2">
                <div className="mb-1 flex items-center gap-2 text-xs">
                  <StatusIcon status={hw.last_detect.ok ? "passed" : "failed"} className="h-3.5 w-3.5" />
                  <span className="font-medium">Last JTAG detect ({hw.last_detect.adapter})</span>
                  <span className="text-muted">{hw.last_detect.state} · {fmtDuration(hw.last_detect.duration_s)}</span>
                </div>
                <KV items={[
                  ["Device", hw.last_detect.device ?? "–"],
                  ["IDCODE", hw.last_detect.idcode ?? "–"],
                  ["Message", hw.last_detect.message],
                ]} />
                {hw.last_detect.raw && (
                  <div className="mt-2">
                    <Collapsible title="Raw output">
                      <Pre maxH="max-h-48">{hw.last_detect.raw}</Pre>
                    </Collapsible>
                  </div>
                )}
              </div>
            )}
          </div>
        </Card>

        <Card title="Program FPGA">
          <div className="space-y-3">
            {!revisionId ? (
              <p className="text-xs text-muted">Select a project revision with a bitstream to program.</p>
            ) : rev.loading && !r ? (
              <Loading />
            ) : (
              <>
                <div className="text-xs">
                  Revision <span className="font-mono font-semibold">r{r?.number}</span>{" "}
                  {hasBitstream ? (
                    <span className="text-muted">bitstream <span className="font-mono" title={r!.build!.bitstream_sha256!}>{shortSha(r!.build!.bitstream_sha256)}</span></span>
                  ) : (
                    <span className="text-warn">has no valid bitstream (build {r?.build_status ?? "not run"})</span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant="primary"
                    busy={requesting}
                    disabled={!hasBitstream || pendingProgram || hw.programming || isRunActive(run)}
                    onClick={() => void program()}
                    title="Requests approval; the board is programmed (volatile SRAM only) after you approve"
                  >
                    Program FPGA
                  </Button>
                  <span className="text-[11px] text-faint">
                    Volatile SRAM programming only (flash is never written). You approve every programming.
                  </span>
                </div>
                {isRunActive(run) && <p className="text-[11px] text-muted">A run is active; it requests programming itself when it reaches that stage.</p>}
                {programStages.length > 0 && (
                  <div className="flex flex-wrap gap-3 text-xs">
                    {programStages.map((s) => (
                      <span key={s.key} className="flex items-center gap-1.5" title={s.summary ?? undefined}>
                        <StatusIcon status={s.status} className="h-3.5 w-3.5" /> {s.label}
                        {s.summary && <span className="max-w-[18rem] truncate text-muted">{s.summary}</span>}
                      </span>
                    ))}
                  </div>
                )}
              </>
            )}
            {programApprovals.map((a) => <ApprovalCard key={a.id} approval={a} />)}
          </div>
        </Card>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card title="Last programming result (as reported)">
          <LastProgram hw={hw} />
        </Card>
        <Card title="macOS xvcd / XVC">
          <XvcPanel hw={hw} />
        </Card>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card title="Adapters">
          <Adapters hw={hw} />
        </Card>
        <Card title={`USB devices (${hw.usb_devices?.length ?? 0})`}>
          {hw.usb_devices?.length ? (
            <table className="w-full text-left text-xs">
              <thead className="text-[10px] uppercase tracking-wide text-faint">
                <tr><th className="py-1 font-medium">VID:PID</th><th className="py-1 font-medium">Product</th><th className="py-1 font-medium">Serial</th><th className="py-1 font-medium">Location</th></tr>
              </thead>
              <tbody className="divide-y divide-line">
                {hw.usb_devices.map((d, i) => (
                  <tr key={i}>
                    <td className="py-1 font-mono">{hex4(d.vendor_id)}:{hex4(d.product_id)}</td>
                    <td className="py-1">{d.product ?? "–"}</td>
                    <td className="py-1 font-mono text-muted">{d.serial ?? "–"}</td>
                    <td className="py-1 font-mono text-muted">{d.location ?? "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-xs text-muted">No relevant USB devices reported. Connect the Basys 3 with its USB-JTAG (PROG/UART) cable and press Detect.</p>
          )}
        </Card>
      </div>
    </div>
  );
}
