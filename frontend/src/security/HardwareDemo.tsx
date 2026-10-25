/**
 * Basys 3 before/after demo for a security case with a trusted board wrapper.
 *
 *   board-level simulation  every demo step on the full Basys 3 top (Verilator); compared
 *                           with the LED pattern the contract requires. Simulation only.
 *   Vivado build            the existing batch flow in the user's container
 *   program                 the existing SHA-bound approval; the board must be connected
 *   webcam evidence         optional: the LEDs the camera saw at each demo step, for the variant
 *                           CircuitLoop just programmed (BoardCamera); nothing physical is inferred
 */

import { useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type BoardSimStep, type FpgaInfo, type SecurityAudit } from "../api/security";
import { Button, Card, Collapsible, ErrorNote, Pill, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";
import { toast } from "../lib/toast";
import { useResource } from "../lib/useResource";
import { useAppState } from "../store/store";
import { BoardCamera, ObservationTable, VERDICT } from "./BoardCamera";

function Leds({ hex }: { hex: string }) {
  const v = parseInt(hex, 16);
  return (
    <span className="inline-flex gap-[3px]" aria-label={`LEDs ${hex}`} title={hex}>
      {Array.from({ length: 16 }, (_, k) => 15 - k).map((i) => (
        <span key={i} className={cx("h-2 w-1.5 rounded-sm", (v >> i) & 1 ? (i >= 14 ? (i === 15 ? "bg-ok" : "bg-bad") : "bg-warn") : "bg-line")} />
      ))}
    </span>
  );
}

export function HardwareDemo({ audit }: { audit: SecurityAudit }) {
  const hw = useAppState((s) => s.hardware);
  const version = JSON.stringify(audit.record.fpga ?? {}) + audit.record.repairs.length;
  const res = useResource<FpgaInfo>(audit.id, version, (sig) => securityApi.fpga(audit.id, sig));
  const [busy, setBusy] = useState<string | null>(null);
  const [camera, setCamera] = useState<string | null>(null);
  const info = res.data;
  if (res.error) return <ErrorNote error={res.error} onRetry={res.reload} />;
  if (!info || !info.available) return null;
  const connected = hw?.state === "connected";
  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try {
      await fn();
      toast("Started; progress appears in the Agent tab and here.", "info");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(null);
    }
  };
  return (
    <Card title="Basys 3 before / after" actions={<Pill tone={connected ? "ok" : "idle"}>{connected ? "board connected" : "board not connected"}</Pill>}>
      <p className="text-xs text-muted">
        Build the vulnerable design and a verifier-accepted fix for the Basys 3, then program each one (with your approval) and run
        the same steps on the board. Pins come from the trusted Basys 3 definition; programming writes SRAM only.
      </p>
      <div className="mt-2 grid gap-x-4 gap-y-0.5 text-2xs text-muted sm:grid-cols-2">
        {info.mapping.map((m) => <div key={m.control}><span className="font-mono text-fg">{m.control}</span> → <span className="font-mono">{m.signal}</span> · {m.text}</div>)}
      </div>
      {camera && (
        <div className="mt-3">
          <BoardCamera auditId={audit.id} variant={camera} label={info.variants.find((x) => x.variant === camera)?.label ?? camera}
                       onClose={() => { setCamera(null); res.reload(); }} />
        </div>
      )}
      <div className="mt-3 space-y-3">
        {info.variants.map((v) => {
          const st = info.state[v.variant];
          const b = st?.build;
          const bitOk = !!b?.ok;
          return (
            <div key={v.variant} className="rounded-md border border-line">
              <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
                <Pill tone={v.kind === "vulnerable" ? "orange" : v.kind === "reference" ? "violet" : "ok"}>
                  {v.kind === "vulnerable" ? "before" : v.kind === "reference" ? "after (reference)" : "after (agent patch)"}
                </Pill>
                <span className="min-w-0 flex-1 text-sm">{v.label}</span>
                <Button size="sm" variant="secondary" busy={busy === `b:${v.variant}`}
                        onClick={() => act(`b:${v.variant}`, () => securityApi.fpgaBuild(audit.id, v.variant))}>
                  {b ? "Rebuild" : "Simulate + build"}
                </Button>
                <Button size="sm" variant="primary" disabled={!bitOk || !connected} busy={busy === `p:${v.variant}`}
                        title={!bitOk ? "build a bitstream first" : !connected ? (hw?.state === "unknown" ? "press Detect in FPGA Lab → Connection first" : "connect the Basys 3") : "asks for your approval before programming"}
                        onClick={() => act(`p:${v.variant}`, () => securityApi.fpgaProgram(audit.id, v.variant))}>
                  Program…
                </Button>
                <Button size="sm" variant="secondary" disabled={!info.provenance?.[v.variant]?.confirmed || camera !== null}
                        title={info.provenance?.[v.variant]?.confirmed ? "record the LEDs the board shows at each demo step"
                          : info.provenance?.[v.variant]?.reason ?? "program this variant first"}
                        onClick={() => setCamera(v.variant)}>
                  Verify with webcam
                </Button>
              </div>
              <div className="space-y-2 px-3 py-2 text-xs">
                {st?.board_sim ? <BoardSim steps={st.board_sim.steps} note={st.board_sim.note} message={st.board_sim.message} />
                  : <p className="text-muted">Not simulated or built yet.</p>}
                {b && (
                  <p className={b.ok ? "text-fg" : "text-muted"}>
                    <span className="text-faint">Vivado: </span>
                    {b.ok ? `bitstream ${b.bitstream_bytes} bytes, WNS ${b.timing?.wns_ns} ns, sha256 ${b.bitstream_sha256?.slice(0, 16)}…`
                      : `${b.status}: ${b.message}`}
                  </p>
                )}
                {(st?.programs ?? []).map((p, i) => (
                  <p key={i} className={p.ok ? "text-ok" : "text-bad"}>
                    Programmed via {p.adapter}: {p.ok ? "done" : p.state} · {p.message}
                  </p>
                ))}
                {bitOk && !info.provenance?.[v.variant]?.confirmed && (
                  <p className="rounded-md border border-warn/30 bg-warn/5 px-2.5 py-1.5 text-warn">
                    Webcam evidence needs this bitstream on the board: {info.provenance?.[v.variant]?.reason ?? "program it first"}.
                    {connected ? " Press Program… (asks for your approval)." : hw?.state === "unknown"
                      ? " Press Detect (FPGA Lab → Connection) to confirm the board, then Program…." : " Connect the Basys 3, then Program…."}
                  </p>
                )}
                {(st?.observations ?? []).slice(-1).map((o) => (
                  <Collapsible key={o.session_id} title={
                    <span className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="text-faint">Webcam evidence</span>
                      <Pill tone={VERDICT[o.verdict].tone}>{VERDICT[o.verdict].label}</Pill>
                      <span className="text-muted">{o.at ? fmtDateTime(o.at) : ""}</span>
                      {(st?.observations?.length ?? 0) > 1 && <span className="text-faint">latest of {st!.observations!.length}</span>}
                    </span>}>
                    <div className="space-y-1.5 p-2">
                      <p className="text-xs">{o.summary}</p>
                      <ObservationTable obs={o} auditId={audit.id} />
                    </div>
                  </Collapsible>
                ))}
              </div>
            </div>
          );
        })}
      </div>
      {info.state.loaded && (
        <p className="mt-2 text-xs text-ok">Last programmed: {info.variants.find((x) => x.variant === info.state.loaded!.variant)?.label}. What the
          board shows is only recorded through “Verify with webcam”; otherwise it is your own observation.</p>
      )}
    </Card>
  );
}

function BoardSim({ steps, note, message }: { steps: BoardSimStep[]; note: string; message: string }) {
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-2xs font-semibold uppercase tracking-wide text-faint">Demo steps · board-level simulation</span>
        <span className="text-2xs text-muted">{message}</span>
      </div>
      <table className="w-full text-2xs">
        <thead className="text-faint">
          <tr><th className="py-0.5 text-left font-normal">step</th><th className="text-left font-normal">set, then hold</th>
            <th className="text-left font-normal">contract requires</th><th className="text-left font-normal">simulated</th><th /></tr>
        </thead>
        <tbody>
          {steps.map((s) => (
            <tr key={s.id} className="border-t border-line align-top">
              <td className="py-1 pr-2"><span className="font-mono text-faint">{s.id}</span> {s.title}</td>
              <td className="pr-2 font-mono text-muted">{Object.entries(s.set).map(([k, v]) => `${k}=${v}`).join(" ")}{s.press ? `, ${s.press}` : ""}</td>
              <td className="pr-2"><Leds hex={s.expected} /> <span className="font-mono text-faint">{s.expected}</span>
                <div className="text-faint">{s.expected_text}</div></td>
              <td className="pr-2"><Leds hex={s.simulated} /> <span className={cx("font-mono", s.matches_contract ? "text-faint" : "text-bad")}>{s.simulated}</span></td>
              <td><Pill tone={s.matches_contract ? "ok" : "bad"}>{s.matches_contract ? "matches" : "differs"}</Pill></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-2xs text-faint">{note} LED15 = granted (green), LED14 = denied (red), LED7–LED0 = data.</p>
    </div>
  );
}
