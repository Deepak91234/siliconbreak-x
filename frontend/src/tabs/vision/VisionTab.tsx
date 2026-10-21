/**
 * Camera verification of the programmed board (docs/api.md vision endpoints).
 *
 * Flow: create a session for the revision -> enable the webcam -> stream JPEG
 * frames ([float64 LE t_ms][JPEG]) to /ws/vision/{id} -> draw a box around the
 * LED row (POST auto-roi, normalised 0..1 coordinates) -> run the guided phases
 * -> finish for a verdict with evidence files. LED states shown here are the
 * server analyser's answers; the browser never infers them itself.
 */

import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api, errorMessage, fileUrl, runsRelative } from "../../api/client";
import type { PhasePlan, VerificationResult, VisionSessionState } from "../../api/types";
import { VisionSocket, type VisionSocketStatus } from "../../api/vision";
import { Button, Card, Collapsible, Empty, ErrorNote, KV, Loading, Pill, Pre, Select, cx } from "../../components/ui";
import { fmtNum, humanize, prettyJson } from "../../lib/format";
import { physicalStatus, revisionStatus, verdict } from "../../lib/status";
import { toast } from "../../lib/toast";
import { useAppState, useStore } from "../../store/store";
import { useRevision } from "../VerilogTab";
import { useCamera } from "./useCamera";

type Box = [number, number, number, number]; // normalised x, y, w, h
type FrameMsg = { t_ms: number; states: Record<string, number | null>; levels: Record<string, number>; quality: Record<string, unknown> };

const MAX_W = 640;
const MAX_H = 480;
const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

function frameDims(size: { w: number; h: number } | null): { w: number; h: number } | null {
  if (!size || !size.w || !size.h) return null;
  const s = Math.min(1, MAX_W / size.w, MAX_H / size.h);
  return { w: Math.round(size.w * s), h: Math.round(size.h * s) };
}

function primitiveEntries(d: Record<string, unknown> | null | undefined, max = 12): Array<[string, string]> {
  if (!d) return [];
  const out: Array<[string, string]> = [];
  for (const [k, v] of Object.entries(d)) {
    if (v === null || typeof v === "number" || typeof v === "string" || typeof v === "boolean") {
      out.push([humanize(k), typeof v === "number" ? fmtNum(v) : String(v)]);
    }
    if (out.length >= max) break;
  }
  return out;
}

// ---------------------------------------------------------------- streaming hook

function useFrameStreamer(sessionId: string | null, video: React.RefObject<HTMLVideoElement>, onState: (s: VisionSessionState) => void) {
  const sockRef = useRef<VisionSocket | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const busyRef = useRef(false);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const onStateRef = useRef(onState);
  onStateRef.current = onState;
  const [status, setStatus] = useState<VisionSocketStatus | "off">("off");
  const [statusDetail, setStatusDetail] = useState<string | null>(null);
  const [lastFrame, setLastFrame] = useState<FrameMsg | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [counters, setCounters] = useState({ sent: 0, dropped: 0 });
  const [fps, setFps] = useState(15);

  const stop = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    sockRef.current?.close();
    sockRef.current = null;
    busyRef.current = false;
    setStatus("off");
  }, []);

  const tick = useCallback(() => {
    const v = video.current;
    const sock = sockRef.current;
    if (!v || !sock || !sock.isOpen || busyRef.current || v.readyState < 2 || !v.videoWidth) return;
    const dims = frameDims({ w: v.videoWidth, h: v.videoHeight });
    if (!dims) return;
    const c = canvasRef.current ?? (canvasRef.current = document.createElement("canvas"));
    if (c.width !== dims.w) c.width = dims.w;
    if (c.height !== dims.h) c.height = dims.h;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const tMs = performance.now();
    ctx.drawImage(v, 0, 0, dims.w, dims.h);
    busyRef.current = true;
    c.toBlob(
      (blob) => {
        if (!blob) {
          busyRef.current = false;
          return;
        }
        blob.arrayBuffer().then(
          (buf) => {
            busyRef.current = false;
            if (sockRef.current === sock) {
              sock.send(tMs, buf);
              setCounters({ sent: sock.sent, dropped: sock.dropped });
            }
          },
          () => {
            busyRef.current = false;
          },
        );
      },
      "image/jpeg",
      0.8,
    );
  }, [video]);

  const start = useCallback(() => {
    if (!sessionId) return;
    stop();
    setServerError(null);
    setLastFrame(null);
    const sock = new VisionSocket(sessionId, {
      onMessage: (m) => {
        if (m.type === "frame") setLastFrame(m as FrameMsg);
        else if (m.type === "state") onStateRef.current(m.session);
        else if (m.type === "error") setServerError(m.message);
      },
      onStatus: (s, detail) => {
        if (sockRef.current !== sock && s !== "closed") return;
        setStatus(s);
        setStatusDetail(detail ?? null);
        if (s === "closed" && sockRef.current === sock) {
          if (timerRef.current) clearInterval(timerRef.current);
          timerRef.current = null;
          sockRef.current = null;
        }
      },
    });
    sockRef.current = sock;
    sock.open();
    timerRef.current = setInterval(tick, Math.round(1000 / fps));
  }, [sessionId, stop, tick, fps]);

  // Changing the frame rate while streaming re-arms the timer.
  useEffect(() => {
    if (!timerRef.current) return;
    clearInterval(timerRef.current);
    timerRef.current = setInterval(tick, Math.round(1000 / fps));
  }, [fps, tick]);

  // A different session (or unmount) ends the stream.
  useEffect(() => stop, [sessionId, stop]);

  return { status, statusDetail, lastFrame, serverError, counters, fps, setFps, start, stop };
}

// ---------------------------------------------------------------- pieces

function LedChips({ frame, session }: { frame: FrameMsg | null; session: VisionSessionState | undefined }) {
  const leds = session?.roi?.leds ?? [];
  if (leds.length === 0) return <p className="text-xs text-muted">No LED regions yet. Draw a box around the LEDs on the preview.</p>;
  const states = frame?.states ?? (session?.live?.states as Record<string, number | null> | undefined) ?? {};
  const levels = frame?.levels ?? (session?.live?.levels as Record<string, number> | undefined) ?? {};
  const sorted = [...leds].sort((a, b) => b.led_index - a.led_index);
  return (
    <div className="flex flex-wrap gap-1.5">
      {sorted.map((l) => {
        const k = String(l.led_index);
        const st = states[k];
        const lv = levels[k];
        return (
          <div
            key={k}
            className={cx(
              "min-w-[3.25rem] rounded-md border px-1.5 py-1 text-center text-[11px]",
              st === 1 ? "border-ok/50 bg-ok/15 text-ok" : st === 0 ? "border-line bg-sunken text-muted" : "border-warn/40 bg-warn/10 text-warn",
            )}
            title={st === undefined ? "no measurement received yet" : st === null ? "analyser could not decide" : st ? "on" : "off"}
          >
            <div className="font-mono font-semibold">LD{l.led_index}</div>
            <div>{st === 1 ? "on" : st === 0 ? "off" : st === null ? "?" : "–"}</div>
            {typeof lv === "number" && <div className="font-mono text-[10px] opacity-80">{lv.toFixed(2)}</div>}
          </div>
        );
      })}
    </div>
  );
}

function PhaseList({ session, busy, onStart, onEnd }: {
  session: VisionSessionState;
  busy: boolean;
  onStart: (p: PhasePlan) => void;
  onEnd: () => void;
}) {
  const runs = (session.phases as Array<{ phase_id: string; run: number; active: boolean; frames: number; summary: Record<string, unknown> | null }> | undefined) ?? [];
  const elapsed = typeof session.phase_elapsed_s === "number" ? session.phase_elapsed_s : null;
  if (session.plan.length === 0) {
    return <Empty title="No camera-verifiable phases">This design's observations cannot be checked with a camera; the verdict will report that.</Empty>;
  }
  return (
    <ol className="space-y-2">
      {session.plan.map((p, i) => {
        const active = session.current_phase === p.id;
        const done = runs.filter((r) => r.phase_id === p.id && !r.active);
        const last = done[done.length - 1];
        const longEnough = elapsed !== null && elapsed >= p.min_duration_s;
        return (
          <li key={p.id} className={cx("rounded-md border px-3 py-2", active ? "border-accent/50 bg-accent/5" : "border-line")}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11px] text-faint">{i + 1}</span>
              <span className="text-sm font-medium">{p.title}</span>
              {active && <Pill tone="accent">recording</Pill>}
              {!active && done.length > 0 && <Pill tone="ok">recorded{done.length > 1 ? ` ×${done.length}` : ""}</Pill>}
              <span className="ml-auto flex gap-1">
                {active ? (
                  <Button size="sm" variant={longEnough ? "primary" : "secondary"} busy={busy} onClick={onEnd} title={`Hold for at least ${p.min_duration_s}s`}>
                    End phase{elapsed !== null ? ` (${elapsed.toFixed(1)}s / ${p.min_duration_s}s)` : ""}
                  </Button>
                ) : (
                  <Button size="sm" busy={busy} disabled={!session.roi} onClick={() => onStart(p)} title={session.roi ? undefined : "Mark the LED regions first"}>
                    {done.length ? "Record again" : "Start"}
                  </Button>
                )}
              </span>
            </div>
            <p className="mt-1 text-xs text-muted">{p.instruction}</p>
            {p.conditions.length > 0 && (
              <ul className="mt-1 list-disc pl-5 text-xs text-fg">
                {p.conditions.map((c, j) => (
                  <li key={j}>{typeof c.instruction === "string" && c.instruction ? c.instruction : `${c.board_signal ?? c.input ?? "input"} = ${c.level ?? "?"}`}</li>
                ))}
              </ul>
            )}
            <div className="mt-1 text-[11px] text-faint">minimum {p.min_duration_s}s{p.observation_id ? ` · observation ${p.observation_id}` : ""}</div>
            {last?.summary && primitiveEntries(last.summary).length > 0 && (
              <KV className="mt-1.5 text-[11px]" items={primitiveEntries(last.summary, 8)} />
            )}
          </li>
        );
      })}
    </ol>
  );
}

export function VerificationView({ result, runsDir }: { result: VerificationResult; runsDir: string | null }) {
  const v = verdict(result.status);
  const images = result.evidence.filter((e) => e.kind === "plot" || e.kind === "snapshot");
  const files = result.evidence.filter((e) => e.kind !== "plot" && e.kind !== "snapshot");
  const link = (path: string) => {
    const rel = runsRelative(path, runsDir);
    return rel ? fileUrl(rel) : null;
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={v.tone} className="text-sm">{v.label}</Pill>
        <span className="text-sm">{result.summary}</span>
      </div>
      {result.requirements.length > 0 && (
        <table className="w-full text-left text-xs">
          <thead className="text-[11px] uppercase tracking-wide text-faint">
            <tr><th className="py-1 pr-2 font-medium">Req</th><th className="py-1 pr-2 font-medium">Text</th><th className="py-1 font-medium">Physical</th></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {result.requirements.map((r) => {
              const ps = physicalStatus(r.physical_status);
              return (
                <tr key={r.id} className="align-top">
                  <td className="py-1 pr-2 font-mono">{r.id}</td>
                  <td className="py-1 pr-2">{r.text}</td>
                  <td className="py-1"><Pill tone={ps.tone}>{ps.label}</Pill></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {primitiveEntries(result.quality).length > 0 && <KV className="text-xs" items={primitiveEntries(result.quality)} />}
      {result.evidence.length === 0 ? (
        <p className="text-xs text-muted">No evidence files were written.</p>
      ) : (
        <div className="space-y-2">
          {images.length > 0 && (
            <div className="grid gap-2 sm:grid-cols-2">
              {images.map((e) => {
                const href = link(e.path);
                return href ? (
                  <a key={e.path} href={href} target="_blank" rel="noreferrer" className="block overflow-hidden rounded-md border border-line bg-sunken">
                    <img src={href} alt={e.name} className="max-h-56 w-full object-contain" loading="lazy" />
                    <div className="px-2 py-1 font-mono text-[11px] text-muted">{e.name}</div>
                  </a>
                ) : (
                  <div key={e.path} className="rounded-md border border-line px-2 py-1 font-mono text-[11px] text-muted" title="outside the runs directory, not served">{e.path}</div>
                );
              })}
            </div>
          )}
          {files.length > 0 && (
            <ul className="flex flex-wrap gap-2 text-xs">
              {files.map((e) => {
                const href = link(e.path);
                return (
                  <li key={e.path}>
                    {href ? (
                      <a className="font-mono text-accent hover:underline" href={href} target="_blank" rel="noreferrer">{e.name}</a>
                    ) : (
                      <span className="font-mono text-muted" title={e.path}>{e.name}</span>
                    )}
                    <span className="ml-1 text-faint">{e.kind}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
      {result.observations.length > 0 && (
        <Collapsible title={`Observations (${result.observations.length})`}>
          <Pre>{prettyJson(result.observations)}</Pre>
        </Collapsible>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- tab

export function VisionTab({ revisionId }: { revisionId: string | null }) {
  const store = useStore();
  const rev = useRevision(revisionId);
  const runsDir = useAppState((s) => (typeof s.environment?.config?.runs_dir === "string" ? s.environment.config.runs_dir : null));
  const sessions = useAppState((s) => s.vision);
  const camera = useCamera();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const [count, setCount] = useState(4);
  const [firstLed, setFirstLed] = useState(0);
  const overlayRef = useRef<HTMLDivElement>(null);

  // Reuse a session for this revision that the store already knows about (events replay on reload).
  useEffect(() => {
    if (!revisionId) {
      setSessionId(null);
      return;
    }
    const mine = Object.values(store.getState().vision).filter((s) => s.revision_id === revisionId);
    const open = mine.filter((s) => s.status !== "finished");
    const pick = open[open.length - 1] ?? null;
    setSessionId((cur) => (cur && sessions[cur]?.revision_id === revisionId ? cur : pick?.id ?? null));
  }, [revisionId]);

  const session = sessionId ? sessions[sessionId] : undefined;
  const put = useCallback((s: VisionSessionState) => store.putVisionSession(s), [store]);
  const stream = useFrameStreamer(sessionId, camera.videoRef, put);

  // Defaults for the LED box from the revision's camera-verifiable observations.
  useEffect(() => {
    const obs = rev.data?.observations ?? [];
    const leds = new Set<number>();
    for (const o of obs) if (o.camera_verifiable) for (const l of o.leds ?? []) leds.add(l);
    if (leds.size > 0) {
      const lo = Math.min(...leds);
      const hi = Math.max(...leds);
      setFirstLed(lo);
      setCount(hi - lo + 1);
    }
  }, [rev.data]);

  const run = async <T,>(label: string, fn: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setActionError(null);
    try {
      return await fn();
    } catch (e) {
      const msg = `${label}: ${errorMessage(e)}`;
      setActionError(msg);
      toast(msg, "error");
      return null;
    } finally {
      setBusy(false);
    }
  };

  const createSession = async () => {
    if (!revisionId) return;
    setCreating(true);
    setActionError(null);
    try {
      const s = await api.createVisionSession(revisionId);
      store.putVisionSession(s);
      setSessionId(s.id);
      setBox(null);
    } catch (e) {
      setActionError(`Could not create a vision session: ${errorMessage(e)}`);
    } finally {
      setCreating(false);
    }
  };

  // ---- box drawing (normalised coordinates of the streamed frame)
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
    if (!drawing || !drag) return;
    const p = toNorm(e);
    setDrag({ ...drag, x1: p.x, y1: p.y });
  };
  const onUp = () => {
    if (!drag) return;
    const x = Math.min(drag.x0, drag.x1);
    const y = Math.min(drag.y0, drag.y1);
    const w = Math.abs(drag.x1 - drag.x0);
    const h = Math.abs(drag.y1 - drag.y0);
    setDrag(null);
    if (w > 0.01 && h > 0.005) {
      setBox([x, y, w, h]);
      setDrawing(false);
    }
  };

  const countOk = Number.isInteger(count) && count >= 1 && count <= 16;
  const firstOk = Number.isInteger(firstLed) && firstLed >= 0 && firstLed + count - 1 <= 15;
  const detectLeds = async () => {
    if (!sessionId || !box) return;
    const s = await run("Auto-ROI", () => api.autoRoi(sessionId, box, count, firstLed));
    if (s) {
      store.putVisionSession(s);
      setBox(null);
      toast(`Marked ${s.roi?.leds.length ?? 0} LED regions`, "ok");
    }
  };
  const startPhase = async (p: PhasePlan) => {
    if (!sessionId) return;
    const s = await run("Start phase", () => api.startPhase(sessionId, p.id));
    if (s) store.putVisionSession(s);
  };
  const endPhase = async () => {
    if (!sessionId) return;
    const s = await run("End phase", () => api.endPhase(sessionId));
    if (s) store.putVisionSession(s);
  };
  const finish = async () => {
    if (!sessionId) return;
    const r = await run("Finish", () => api.finishVision(sessionId));
    if (r) {
      stream.stop();
      toast(`Vision verdict: ${verdict(r.status).label}`, r.status === "passed" ? "ok" : "info");
      try {
        store.putVisionSession(await api.visionSession(sessionId));
      } catch {
        /* the vision.updated event carries it as well */
      }
    }
  };

  const shownBox: Box | null = drag
    ? [Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0)]
    : box;
  const liveStates = stream.lastFrame?.states ?? (session?.live?.states as Record<string, number | null> | undefined) ?? {};
  const dims = frameDims(camera.size);
  const previous = rev.data?.verification ?? null;
  const streaming = stream.status === "open" || stream.status === "connecting";

  if (!revisionId) {
    return <Empty title="No revision to verify">Camera verification needs a generated, built and programmed revision.</Empty>;
  }
  if (rev.loading && !rev.data) return <Loading label="Loading revision…" />;
  if (rev.error && !rev.data) return <ErrorNote error={rev.error} onRetry={rev.reload} />;
  const r = rev.data;
  const rs = r ? revisionStatus(r.status) : null;
  const verifiable = (r?.observations ?? []).filter((o) => o.camera_verifiable);
  const notVerifiable = (r?.observations ?? []).filter((o) => !o.camera_verifiable);

  return (
    <div className="grid gap-3 2xl:grid-cols-[minmax(0,1fr)_24rem]">
      <div className="min-w-0 space-y-3">
        <Card
          title="Camera"
          actions={
            <div className="flex flex-wrap items-center gap-1.5">
              {camera.devices.length > 1 && (
                <Select value={camera.deviceId} onChange={(id) => void camera.start(id)} className="max-w-[12rem]" title="Camera">
                  {camera.devices.map((d, i) => (
                    <option key={d.deviceId || i} value={d.deviceId}>{d.label || `Camera ${i + 1}`}</option>
                  ))}
                </Select>
              )}
              {camera.status === "live" ? (
                <Button size="sm" variant="ghost" onClick={() => { stream.stop(); camera.stop(); }}>Disable camera</Button>
              ) : (
                <Button size="sm" variant="primary" busy={camera.status === "requesting"} onClick={() => void camera.start(camera.deviceId || undefined)}>
                  Enable camera
                </Button>
              )}
            </div>
          }
        >
          {camera.error && <div className="mb-2 rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{camera.error}</div>}
          <div className={cx("relative mx-auto max-w-[640px] overflow-hidden rounded-md bg-sunken", camera.status !== "live" && "hidden")}>
            <video ref={camera.videoRef} className="block h-auto w-full" muted playsInline />
            <div
              ref={overlayRef}
              className={cx("absolute inset-0 touch-none select-none", drawing ? "cursor-crosshair" : "pointer-events-none")}
              onPointerDown={onDown}
              onPointerMove={onMove}
              onPointerUp={onUp}
              onPointerCancel={() => setDrag(null)}
            >
              {session?.roi?.leds.map((l) => {
                const st = liveStates[String(l.led_index)];
                return (
                  <div
                    key={l.led_index}
                    className={cx("absolute border-2", st === 1 ? "border-ok bg-ok/20" : st === 0 ? "border-info/70" : "border-warn/80")}
                    style={{ left: `${l.x * 100}%`, top: `${l.y * 100}%`, width: `${l.w * 100}%`, height: `${l.h * 100}%` }}
                  >
                    <span className="absolute -top-4 left-0 rounded bg-black/60 px-0.5 font-mono text-[9px] leading-tight text-white">{l.led_index}</span>
                  </div>
                );
              })}
              {shownBox && (
                <div
                  className="absolute border-2 border-dashed border-accent bg-accent/10"
                  style={{ left: `${shownBox[0] * 100}%`, top: `${shownBox[1] * 100}%`, width: `${shownBox[2] * 100}%`, height: `${shownBox[3] * 100}%` }}
                />
              )}
            </div>
          </div>
          {camera.status !== "live" && !camera.error && (
            <Empty title={camera.status === "requesting" ? "Waiting for camera permission…" : "Camera off"}>
              Point a webcam at the Basys 3 so the LED row is clearly visible, then press Enable camera. Frames are sent only to the local CircuitLoop backend.
            </Empty>
          )}
          {camera.status === "live" && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
              <span>
                camera {camera.size ? `${camera.size.w}×${camera.size.h}` : "–"}
                {dims && camera.size && (dims.w !== camera.size.w || dims.h !== camera.size.h) ? ` → streamed ${dims.w}×${dims.h}` : ""}
              </span>
              {session && (
                <>
                  <span className="text-faint">·</span>
                  {streaming ? (
                    <Button size="sm" variant="ghost" onClick={stream.stop}>Stop streaming</Button>
                  ) : (
                    <Button size="sm" variant="primary" onClick={stream.start} disabled={session.status === "finished"}>Start streaming</Button>
                  )}
                  <label className="flex items-center gap-1">
                    rate
                    <Select value={String(stream.fps)} onChange={(v) => stream.setFps(Number(v))}>
                      {[5, 10, 15, 20, 30].map((f) => <option key={f} value={f}>{f} fps</option>)}
                    </Select>
                  </label>
                  <Pill tone={stream.status === "open" ? "ok" : stream.status === "connecting" ? "accent" : stream.status === "error" ? "bad" : "idle"} title={stream.statusDetail ?? undefined}>
                    socket {stream.status}
                  </Pill>
                  <span>sent {stream.counters.sent} · dropped {stream.counters.dropped}</span>
                  {typeof session.live?.fps === "number" && <span>server {session.live.fps.toFixed(1)} fps · {session.live.frames ?? 0} frames</span>}
                </>
              )}
            </div>
          )}
          {stream.serverError && <p className="mt-1 text-xs text-bad">Server: {stream.serverError}</p>}
        </Card>

        {session && (
          <Card title="LED regions">
            <div className="space-y-2">
              <p className="text-xs text-muted">
                Draw one box tightly around the LEDs to watch (left = highest LED number on the Basys 3). The server splits it into equal cells and snaps each to the lit blob in the last streamed frame.
              </p>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Button size="sm" variant={drawing ? "warn" : "secondary"} disabled={camera.status !== "live"} onClick={() => setDrawing((d) => !d)}>
                  {drawing ? "Cancel drawing" : box ? "Redraw box" : "Draw box"}
                </Button>
                <label className="flex items-center gap-1">
                  LEDs in box
                  <input type="number" min={1} max={16} value={count} onChange={(e) => setCount(Number(e.target.value))} className={cx("h-7 w-14 rounded-md border bg-sunken px-1.5 text-xs", countOk ? "border-line" : "border-bad")} />
                </label>
                <label className="flex items-center gap-1">
                  rightmost LED index
                  <input type="number" min={0} max={15} value={firstLed} onChange={(e) => setFirstLed(Number(e.target.value))} className={cx("h-7 w-14 rounded-md border bg-sunken px-1.5 text-xs", firstOk ? "border-line" : "border-bad")} />
                </label>
                <span className="text-faint">covers LD{firstLed}..LD{firstLed + count - 1}</span>
                <Button size="sm" variant="primary" busy={busy} disabled={!box || !countOk || !firstOk || stream.status !== "open"} onClick={detectLeds}
                  title={stream.status !== "open" ? "Start streaming first: the server locates LEDs in the last frame it received" : undefined}>
                  Detect LEDs in box
                </Button>
              </div>
              {drawing && <p className="text-xs text-accent">Drag on the camera preview to draw the box.</p>}
              <LedChips frame={stream.lastFrame} session={session} />
              {stream.lastFrame && primitiveEntries(stream.lastFrame.quality).length > 0 && (
                <KV className="text-[11px]" items={primitiveEntries(stream.lastFrame.quality, 8)} />
              )}
            </div>
          </Card>
        )}
      </div>

      <div className="min-w-0 space-y-3">
        <Card title="Vision session" actions={session ? <Pill tone={session.status === "finished" ? "ok" : session.status === "running" ? "accent" : "idle"}>{session.status}</Pill> : null}>
          <div className="space-y-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-muted">Revision</span>
              <span className="font-mono">{r ? `r${r.number}` : revisionId}</span>
              {rs && <Pill tone={rs.tone}>{rs.label}</Pill>}
            </div>
            {r && r.status !== "programmed" && r.status !== "verified" && r.status !== "verify_failed" && (
              <p className="text-warn">This revision is not recorded as programmed. Program the board from the Hardware tab first, or the camera will be watching a different design.</p>
            )}
            {verifiable.length > 0 ? (
              <p className="text-muted">{verifiable.length} camera-verifiable observation{verifiable.length === 1 ? "" : "s"}{notVerifiable.length ? `, ${notVerifiable.length} not observable by camera` : ""}.</p>
            ) : r ? (
              <p className="text-muted">No camera-verifiable observations in this revision.</p>
            ) : null}
            {!session ? (
              <Button variant="primary" busy={creating} onClick={createSession}>Start vision session</Button>
            ) : (
              <div className="flex flex-wrap gap-2">
                <span className="font-mono text-faint">{session.id}</span>
                {session.status !== "finished" && (
                  <Button size="sm" variant="ok" busy={busy} onClick={finish} disabled={session.current_phase !== null} title={session.current_phase ? "End the running phase first" : undefined}>
                    Finish and get verdict
                  </Button>
                )}
                <Button size="sm" variant="ghost" busy={creating} onClick={() => { stream.stop(); void createSession(); }}>New session</Button>
              </div>
            )}
            {actionError && <p className="text-bad">{actionError}</p>}
          </div>
        </Card>

        {session && (
          <Card title="Guided phases">
            <PhaseList session={session} busy={busy} onStart={startPhase} onEnd={endPhase} />
          </Card>
        )}

        {session?.result ? (
          <Card title="Verdict">
            <VerificationView result={session.result} runsDir={runsDir} />
          </Card>
        ) : previous ? (
          <Card title="Last recorded verdict">
            <VerificationView result={previous} runsDir={runsDir} />
          </Card>
        ) : (
          <Card title="Verdict">
            <p className="text-xs text-muted">No camera verification has been recorded for this revision.</p>
          </Card>
        )}
      </div>
    </div>
  );
}
