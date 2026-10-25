/**
 * Streams webcam frames to /ws/vision/{session_id} ([float64 LE t_ms][JPEG]) and
 * hands back the server's per-frame LED answers. Shared by the design Vision tab
 * and the security demo's board observation; the browser never infers LED states.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { VisionSessionState } from "../../api/types";
import { VisionSocket, type VisionSocketStatus } from "../../api/vision";

export type Box = [number, number, number, number]; // normalised x, y, w, h
export type FrameMsg = { t_ms: number; states: Record<string, number | null>; levels: Record<string, number>; quality: Record<string, unknown> };

export const MAX_W = 640;
export const MAX_H = 480;
export const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

export function frameDims(size: { w: number; h: number } | null): { w: number; h: number } | null {
  if (!size || !size.w || !size.h) return null;
  const s = Math.min(1, MAX_W / size.w, MAX_H / size.h);
  return { w: Math.round(size.w * s), h: Math.round(size.h * s) };
}

// ---------------------------------------------------------------- streaming hook

export function useFrameStreamer<S = VisionSessionState>(sessionId: string | null, video: React.RefObject<HTMLVideoElement>, onState: (s: S) => void) {
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
        else if (m.type === "state") onStateRef.current(m.session as unknown as S);
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
