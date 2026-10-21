/**
 * `/ws/vision/{session_id}` client.
 *
 * Wire format (docs/api.md): every client message is binary,
 *   [8 bytes little-endian float64 t_ms][JPEG bytes]
 * where t_ms is `performance.now()` at capture. The server answers with JSON
 * `frame` / `state` / `error` messages. The camera is the only data source;
 * nothing here produces LED states - they come back from the server's analyser.
 */

import type { VisionServerMessage } from "./types";

export const FRAME_HEADER_BYTES = 8;

/** Pack one frame. Exported for tests (the byte layout is a contract). */
export function encodeFrame(tMs: number, jpeg: ArrayBuffer | Uint8Array): ArrayBuffer {
  const body = jpeg instanceof Uint8Array ? jpeg : new Uint8Array(jpeg);
  const out = new ArrayBuffer(FRAME_HEADER_BYTES + body.byteLength);
  new DataView(out).setFloat64(0, tMs, true);
  new Uint8Array(out, FRAME_HEADER_BYTES).set(body);
  return out;
}

/** Inverse of encodeFrame (used by tests to prove the layout). */
export function decodeFrame(buf: ArrayBuffer): { tMs: number; jpeg: Uint8Array } {
  const tMs = new DataView(buf).getFloat64(0, true);
  return { tMs, jpeg: new Uint8Array(buf, FRAME_HEADER_BYTES) };
}

export function parseVisionMessage(raw: unknown): VisionServerMessage | null {
  if (typeof raw !== "string") return null;
  let m: unknown;
  try {
    m = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!m || typeof m !== "object") return null;
  const t = (m as { type?: unknown }).type;
  if (t === "frame" || t === "state" || t === "error") return m as VisionServerMessage;
  return null;
}

export function visionUrl(sessionId: string, loc: { protocol: string; host: string } = window.location): string {
  const proto = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${loc.host}/ws/vision/${encodeURIComponent(sessionId)}`;
}

export type VisionSocketStatus = "connecting" | "open" | "closed" | "error";

/**
 * Thin wrapper with backpressure: `send` refuses a frame while the socket's
 * send buffer is above `maxBufferedBytes`, so a slow server sees a lower frame
 * rate rather than an ever-growing queue (and stale frames).
 */
export class VisionSocket {
  private ws: WebSocket | null = null;
  private closedByUser = false;
  sent = 0;
  dropped = 0;

  constructor(
    private readonly sessionId: string,
    private readonly handlers: {
      onMessage: (m: VisionServerMessage) => void;
      onStatus: (s: VisionSocketStatus, detail?: string) => void;
    },
    private readonly maxBufferedBytes = 1_500_000,
  ) {}

  open(): void {
    this.closedByUser = false;
    this.handlers.onStatus("connecting");
    const ws = new WebSocket(visionUrl(this.sessionId));
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => this.ws === ws && this.handlers.onStatus("open");
    ws.onerror = () => this.ws === ws && this.handlers.onStatus("error", "vision socket error");
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.handlers.onStatus("closed", this.closedByUser ? "closed" : `closed (code ${ev.code}${ev.reason ? `: ${ev.reason}` : ""})`);
    };
    ws.onmessage = (ev) => {
      const m = parseVisionMessage(ev.data);
      if (m) this.handlers.onMessage(m);
    };
  }

  get isOpen(): boolean {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  get buffered(): number {
    return this.ws?.bufferedAmount ?? 0;
  }

  /** Returns false (and counts a drop) when the frame was not sent. */
  send(tMs: number, jpeg: ArrayBuffer): boolean {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > this.maxBufferedBytes) {
      this.dropped += 1;
      return false;
    }
    ws.send(encodeFrame(tMs, jpeg));
    this.sent += 1;
    return true;
  }

  close(): void {
    this.closedByUser = true;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      try {
        ws.close(1000, "client stop");
      } catch {
        /* ignore */
      }
    }
    this.handlers.onStatus("closed", "closed");
  }
}
