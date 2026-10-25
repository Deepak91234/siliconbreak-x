/**
 * Webcam evidence for the Basys 3 security demo: the LEDs the board actually showed
 * at each demo step, measured from camera frames by the backend's LED analyser.
 *
 * Only a variant CircuitLoop itself just programmed can be observed (the camera cannot
 * tell which bitstream is loaded). Flow: start a session -> camera on, frames stream to
 * /ws/vision/{id} -> draw a box around LED15..LED0 -> for each step set the switches,
 * hold the button and press Capture -> Finish stores the per-step observations, the
 * annotated snapshots and the raw per-frame levels with the audit.
 */

import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type BoardObservation, type CameraSession, type ObservationVerdict, type ObservedStep } from "../api/security";
import { Button, Card, Empty, Pill, Select, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";
import type { Tone } from "../lib/status";
import { toast } from "../lib/toast";
import { useCamera } from "../tabs/vision/useCamera";
import { type Box, clamp01, useFrameStreamer } from "../tabs/vision/useFrameStreamer";

const LEAD_S = 2;      // time to get a hand on the button before recording starts
const RECORD_S = 3;    // minimum recording; it continues until the server has enough frames
const WANT_FRAMES = 24;
const MAX_EXTRA_S = 9;

export const VERDICT: Record<ObservationVerdict, { label: string; tone: Tone }> = {
  violation_observed: { label: "violation observed on the board", tone: "bad" },
  contract_met: { label: "contract met on the board", tone: "ok" },
  unexpected: { label: "board differs from simulation", tone: "warn" },
  inconclusive: { label: "inconclusive", tone: "orange" },
};

export function LedRow({ hex, size = "sm" }: { hex: string | null | undefined; size?: "sm" | "md" }) {
  if (!hex) return <span className="font-mono text-2xs text-faint">–</span>;
  const v = parseInt(hex, 16);
  return (
    <span className="inline-flex gap-[3px]" aria-label={`LEDs ${hex}`} title={hex}>
      {Array.from({ length: 16 }, (_, k) => 15 - k).map((i) => (
        <span key={i} className={cx(size === "md" ? "h-3 w-2" : "h-2 w-1.5", "rounded-sm",
          (v >> i) & 1 ? (i >= 14 ? (i === 15 ? "bg-ok" : "bg-bad") : "bg-warn") : "bg-line")} />
      ))}
    </span>
  );
}

function stepPill(s: ObservedStep) {
  if (s.status === "not_run") return <Pill tone="idle">not captured</Pill>;
  if (s.status === "inconclusive") return <Pill tone="orange" title={(s.reasons ?? []).join("; ")}>inconclusive</Pill>;
  if (s.step_id === "DARK") return <Pill tone={s.matches_contract ? "ok" : "warn"}>{s.matches_contract ? "all dark" : "not dark"}</Pill>;
  return <Pill tone={s.matches_contract ? "ok" : "bad"}>{s.matches_contract ? "meets contract" : "violates contract"}</Pill>;
}

function howTo(step: { id: string; set: Record<string, number>; press: string | null }): string {
  if (step.id === "DARK") return "Release every button (switches as they are). Gives the camera an all-off reference.";
  const sw = Object.entries(step.set).map(([k, v]) => `${k} ${v ? "up" : "down"}`).join(", ");
  return `${sw}${step.press ? `, then hold ${step.press}` : ""}`;
}

/** Stored observation: per-step table with the annotated snapshot the analyser used. */
export function ObservationTable({ obs, auditId }: { obs: BoardObservation; auditId: string }) {
  const [img, setImg] = useState<string | null>(null);
  return (
    <div className="space-y-1.5">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] text-2xs">
          <thead className="text-faint">
            <tr><th className="py-0.5 text-left font-normal">step</th><th className="text-left font-normal">camera saw</th>
              <th className="text-left font-normal">contract requires</th><th className="text-left font-normal">simulated</th><th /></tr>
          </thead>
          <tbody>
            {obs.steps.map((s) => (
              <tr key={s.step_id} className="border-t border-line align-top">
                <td className="py-1 pr-2"><span className="font-mono text-faint">{s.step_id}</span> {s.title}
                  {s.status === "inconclusive" && <div className="text-orange">{(s.reasons ?? []).join("; ")}</div>}</td>
                <td className="whitespace-nowrap pr-2"><LedRow hex={s.observed} /><div className="font-mono text-faint">{s.observed ?? "?"}</div></td>
                <td className="whitespace-nowrap pr-2"><LedRow hex={s.expected} /><div className="font-mono text-faint">{s.expected}</div></td>
                <td className="whitespace-nowrap pr-2"><LedRow hex={s.simulated} /><div className="font-mono text-faint">{s.simulated ?? "–"}
                  {s.status === "observed" && !s.matches_simulation && <span className="ml-1 text-warn">≠ camera</span>}</div></td>
                <td className="whitespace-nowrap">{stepPill(s)}
                  {s.snapshot && obs.dir && (
                    <div><button type="button" className="text-accent hover:underline" onClick={() => setImg(img === s.snapshot ? null : s.snapshot!)}>
                      {img === s.snapshot ? "hide snapshot" : "snapshot"}
                    </button></div>
                  )}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {img && obs.dir && (
        <img src={securityApi.cameraImageUrl(auditId, `${obs.dir}/${img}`)} alt={`camera snapshot ${img}`}
             className="max-w-full rounded border border-line" />
      )}
      <p className="text-2xs text-faint">
        {obs.frames_recorded} frames · {obs.method}. Bitstream {obs.programmed.bitstream_sha256?.slice(0, 12)}… was programmed by
        SiliconBreak X {obs.programmed.at ? fmtDateTime(obs.programmed.at) : ""} (approval {obs.programmed.approval_id}).
        {obs.dir ? ` Evidence: ${obs.dir}/observation.json, frames.csv, snapshots/.` : ""}
      </p>
    </div>
  );
}

export function BoardCamera({ auditId, variant, label, onClose }: { auditId: string; variant: string; label: string; onClose: () => void }) {
  const camera = useCamera();
  const [sess, setSess] = useState<CameraSession | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const [capture, setCapture] = useState<{ step: string; phase: "lead" | "rec" | "more"; left: number } | null>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const timers = useRef<number[]>([]);
  // The panel opens inside a long page: bring it into view so the click visibly does something.
  useEffect(() => { rootRef.current?.scrollIntoView({ block: "start" }); }, []);

  const put = useCallback((s: CameraSession) => setSess((cur) => (cur && s.id !== cur.id ? cur : s)), []);
  const stream = useFrameStreamer<CameraSession>(sess?.id ?? null, camera.videoRef, put);

  useEffect(() => () => timers.current.forEach((t) => window.clearTimeout(t)), []);
  // Stream as soon as both the camera and the session are up.
  useEffect(() => {
    if (sess && sess.status !== "finished" && camera.status === "live" && stream.status === "off") stream.start();
  }, [sess?.id, sess?.status, camera.status, stream.status]);

  const call = async <T,>(what: string, fn: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setErr(null);
    try {
      return await fn();
    } catch (e) {
      setErr(`${what}: ${errorMessage(e)}`);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const begin = async () => {
    const s = await call("Could not start the camera session", () => securityApi.cameraCreate(auditId, variant));
    if (s) setSess(s);
  };
  // Turn the camera on once the session view (and its <video>) is mounted.
  useEffect(() => {
    if (sess?.id && camera.status === "idle") void camera.start(camera.deviceId || undefined);
  }, [sess?.id]);

  const toNorm = (e: ReactPointerEvent) => {
    const r = overlayRef.current!.getBoundingClientRect();
    return { x: clamp01((e.clientX - r.left) / r.width), y: clamp01((e.clientY - r.top) / r.height) };
  };
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!drawing) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const p = toNorm(e);
    setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (drawing && drag) setDrag({ ...drag, ...((p) => ({ x1: p.x, y1: p.y }))(toNorm(e)) });
  };
  const onUp = () => {
    if (!drag) return;
    const b: Box = [Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0)];
    setDrag(null);
    if (b[2] > 0.05 && b[3] > 0.005) {
      setBox(b);
      setDrawing(false);
    }
  };

  const detect = async () => {
    if (!sess || !box) return;
    const s = await call("Detect LEDs", () => securityApi.cameraAutoRoi(sess.id, box));
    if (s) {
      setSess(s);
      setBox(null);
    }
  };

  const tickDown = (step: string, phase: "lead" | "rec", left: number, then: () => void) => {
    setCapture({ step, phase, left });
    if (left <= 0) return then();
    timers.current.push(window.setTimeout(() => tickDown(step, phase, left - 1, then), 1000));
  };
  const runCapture = (stepId: string) => {
    if (!sess) return;
    const id = sess.id;
    tickDown(stepId, "lead", LEAD_S, async () => {
      const s = await call(`Start ${stepId}`, () => securityApi.cameraStart(id, stepId));
      if (!s) return setCapture(null);
      setSess(s);
      tickDown(stepId, "rec", RECORD_S, async () => {
        // A slow machine or a throttled background tab sends fewer frames: keep recording until enough arrived.
        for (let t = 0; t < MAX_EXTRA_S * 2; t++) {
          const cur = await securityApi.camera(id).catch(() => null);
          if (!cur || cur.active_frames >= WANT_FRAMES) break;
          setCapture({ step: stepId, phase: "more", left: cur.active_frames });
          await new Promise((res) => { timers.current.push(window.setTimeout(res, 500)); });
        }
        const r = await call(`End ${stepId}`, () => securityApi.cameraEnd(id));
        setCapture(null);
        if (r) setSess(r.session);
      });
    });
  };

  const finish = async () => {
    if (!sess) return;
    const r = await call("Finish", () => securityApi.cameraFinish(sess.id));
    if (r) {
      stream.stop();
      camera.stop();
      setSess({ ...sess, status: "finished", result: r });
      toast(`Webcam evidence recorded: ${VERDICT[r.verdict].label}`, r.verdict === "inconclusive" ? "info" : "ok");
    }
  };

  const shownBox: Box | null = drag
    ? [Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0)] : box;
  const live = stream.lastFrame?.states ?? sess?.live?.states ?? {};
  const flags = ((stream.lastFrame?.quality ?? sess?.live?.quality)?.flags as string[] | undefined) ?? [];
  const captured = sess ? Object.keys(sess.step_results).length : 0;

  return (
    <div ref={rootRef} className="scroll-mt-4">
    <Card title={<span>Webcam evidence · <span className="font-normal text-muted">{label}</span></span>}
          actions={<Button size="sm" variant="ghost" onClick={() => { stream.stop(); camera.stop(); onClose(); }}>Close</Button>}>
      {!sess && (
        <div className="space-y-2 text-xs text-muted">
          <p>
            Records what the board's LEDs actually show for each demo step, using the webcam. The backend measures every LED in every
            frame; a step only counts when all 16 LEDs were read consistently. Results are stored with this audit as physical evidence,
            next to the simulation and the contract.
          </p>
          <p>Point the webcam at the Basys 3 so LED15…LED0 are in view and not washed out, then start.</p>
          <Button size="sm" variant="primary" busy={busy} onClick={begin}>Start webcam session</Button>
        </div>
      )}
      {err && <p className="mt-2 rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{err}</p>}
      {sess && sess.status !== "finished" && (
        <div className="space-y-3">
          <p className="text-2xs text-ok">
            SiliconBreak X programmed this bitstream ({sess.programmed.bitstream_sha256?.slice(0, 12)}…) {sess.programmed.at ? fmtDateTime(sess.programmed.at) : ""}.
            If the board was power-cycled since, it no longer holds it; the comparison with simulation will show that.
          </p>
          {camera.error && <p className="rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{camera.error}</p>}
          <div className={cx("relative mx-auto max-w-[640px] overflow-hidden rounded-md bg-sunken", camera.status !== "live" && "hidden")}>
            <video ref={camera.videoRef} className="block h-auto w-full" muted playsInline />
            <div ref={overlayRef} className={cx("absolute inset-0 touch-none select-none", drawing ? "cursor-crosshair" : "pointer-events-none")}
                 onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={() => setDrag(null)}>
              {sess.roi?.leds.map((l) => {
                const st = live[String(l.led_index)];
                return (
                  <div key={l.led_index} className={cx("absolute border", st === 1 ? "border-ok bg-ok/25" : st === 0 ? "border-info/70" : "border-warn/80")}
                       style={{ left: `${l.x * 100}%`, top: `${l.y * 100}%`, width: `${l.w * 100}%`, height: `${l.h * 100}%` }} />
                );
              })}
              {shownBox && <div className="absolute border-2 border-dashed border-accent bg-accent/10"
                                style={{ left: `${shownBox[0] * 100}%`, top: `${shownBox[1] * 100}%`, width: `${shownBox[2] * 100}%`, height: `${shownBox[3] * 100}%` }} />}
            </div>
            {capture && (
              <div className="absolute inset-x-0 top-0 bg-black/70 px-3 py-1.5 text-center text-sm font-semibold text-white">
                {capture.phase === "lead" ? `${capture.step}: hold the button now… ${capture.left}`
                  : capture.phase === "rec" ? `${capture.step}: recording, keep holding… ${capture.left}`
                  : `${capture.step}: keep holding, few frames are arriving (${capture.left}/${WANT_FRAMES})`}
              </div>
            )}
          </div>
          {camera.status !== "live" && !camera.error && (
            <Empty title={camera.status === "requesting" ? "Waiting for camera permission…" : "Camera off"}>
              <Button size="sm" variant="primary" onClick={() => void camera.start(camera.deviceId || undefined)}>Enable camera</Button>
            </Empty>
          )}
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            {camera.devices.length > 1 && (
              <Select value={camera.deviceId} onChange={(id) => void camera.start(id)} className="max-w-[12rem]" title="Camera">
                {camera.devices.map((d, i) => <option key={d.deviceId || i} value={d.deviceId}>{d.label || `Camera ${i + 1}`}</option>)}
              </Select>
            )}
            <Pill tone={stream.status === "open" ? "ok" : stream.status === "connecting" ? "accent" : "idle"} title={stream.statusDetail ?? undefined}>
              stream {stream.status}
            </Pill>
            {sess.roi && <span>{sess.frames_recorded} frames recorded</span>}
            {flags.filter((f) => f !== "no_roi").map((f) => <Pill key={f} tone="warn">{f.replace(/_/g, " ")}</Pill>)}
          </div>
          {stream.serverError && <p className="text-xs text-bad">Server: {stream.serverError}</p>}

          <section className="rounded-md border border-line p-2.5">
            <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium">1. Frame the LEDs</span>
              <Button size="sm" variant={drawing ? "warn" : "secondary"} disabled={camera.status !== "live"} onClick={() => setDrawing((d) => !d)}>
                {drawing ? "Cancel" : box || sess.roi ? "Redraw box" : "Draw box"}
              </Button>
              <Button size="sm" variant="primary" busy={busy && !capture} disabled={!box || stream.status !== "open"} onClick={detect}
                      title={stream.status !== "open" ? "waiting for the camera stream" : undefined}>Detect 16 LEDs</Button>
              {sess.roi && <Pill tone="ok">{sess.roi.leds.length} LEDs framed</Pill>}
            </div>
            <p className="text-2xs text-muted">
              {drawing ? "Drag on the preview from just left of LED15 to just right of LED0." :
                "One box tightly around the whole LED row, LED15 (left) to LED0 (right). The server splits it into 16 cells and snaps each to its LED."}
            </p>
            {sess.roi && (
              <div className="mt-2 flex flex-wrap gap-1" aria-label="live LED states">
                {Array.from({ length: 16 }, (_, k) => 15 - k).map((i) => {
                  const st = live[String(i)];
                  return (
                    <span key={i} title={st === 1 ? "on" : st === 0 ? "off" : "not decided yet"}
                          className={cx("w-8 rounded border py-0.5 text-center font-mono text-[10px]",
                            st === 1 ? "border-ok/50 bg-ok/15 text-ok" : st === 0 ? "border-line bg-sunken text-muted" : "border-warn/40 text-warn")}>
                      {i}
                    </span>
                  );
                })}
                <span className="self-center text-2xs text-faint">live (calibrates as LEDs change)</span>
              </div>
            )}
          </section>

          <section className="rounded-md border border-line p-2.5">
            <div className="mb-1.5 text-xs font-medium">2. Capture each step <span className="font-normal text-muted">
              (press Capture, then hold the button until the banner on the preview clears, about {LEAD_S + RECORD_S} s)</span></div>
            <ol className="space-y-1.5">
              {sess.steps.map((st) => {
                const r = sess.step_results[st.id];
                const active = capture?.step === st.id;
                return (
                  <li key={st.id} className={cx("rounded border px-2.5 py-1.5", active ? "border-accent/60 bg-accent/5" : "border-line")}>
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="font-mono text-faint">{st.id === "DARK" ? "0" : st.id}</span>
                      <span className="min-w-0 flex-1">{st.title}</span>
                      {r && stepPill(r)}
                      <Button size="sm" variant={r ? "ghost" : "secondary"} disabled={!sess.roi || !!capture || busy}
                              title={!sess.roi ? "frame the LEDs first" : undefined} onClick={() => runCapture(st.id)}>
                        {r ? "Capture again" : "Capture"}
                      </Button>
                    </div>
                    <div className="mt-0.5 flex flex-wrap gap-x-4 gap-y-0.5 text-2xs text-muted">
                      <span className="text-fg">{howTo(st)}</span>
                      <span>contract <LedRow hex={st.expect_leds} /></span>
                      <span>simulated <LedRow hex={st.simulated} /></span>
                      {r && <span>camera saw <LedRow hex={r.observed} /> <span className="font-mono">{r.observed ?? "?"}</span></span>}
                    </div>
                    {r?.status === "inconclusive" && <p className="mt-0.5 text-2xs text-orange">{(r.reasons ?? []).join("; ")}</p>}
                  </li>
                );
              })}
            </ol>
          </section>
          <div className="flex items-center gap-2">
            <Button variant="primary" size="sm" busy={busy && !capture} disabled={captured === 0 || !!capture} onClick={finish}>
              Finish &amp; record evidence
            </Button>
            <span className="text-2xs text-muted">{captured}/{sess.steps.length} steps captured. Steps not captured are reported as such; nothing is inferred.</span>
          </div>
        </div>
      )}
      {sess?.status === "finished" && sess.result && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={VERDICT[sess.result.verdict].tone}>{VERDICT[sess.result.verdict].label}</Pill>
            <span className="text-xs">{sess.result.summary}</span>
          </div>
          <ObservationTable obs={sess.result} auditId={auditId} />
        </div>
      )}
    </Card>
    </div>
  );
}
