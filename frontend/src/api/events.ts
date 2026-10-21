/**
 * `/ws/events` client: resumable, batched, reconnecting.
 *
 * The server replays persisted events with `seq > after` and then streams live
 * ones (docs/api.md). We remember the highest `seq` seen and reconnect with
 * `?after=<seq>`, so a dropped connection loses nothing that was persisted.
 * Because the replay is capped (2000 events) the owner should also resync from
 * the REST API after every (re)connect - `onOpen` tells it when.
 *
 * Events are delivered in batches (one flush per `flushMs`) so a replay of
 * thousands of log lines causes a handful of renders, not thousands.
 */

import type { CLEvent } from "./types";

export type SocketStatus = "idle" | "connecting" | "open" | "closed";

export interface SocketInfo {
  status: SocketStatus;
  lastSeq: number;
  attempts: number;
  retryInMs: number | null;
  lastError: string | null;
  connectedAt: number | null;
}

interface WsLike {
  readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: { code?: number; reason?: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  close(code?: number, reason?: string): void;
}

export type WsFactory = (url: string) => WsLike;

export interface EventStreamOptions {
  /** Absolute ws:// URL for a given resume point. */
  url: (after: number) => string;
  onEvents: (events: CLEvent[]) => void;
  onInfo?: (info: SocketInfo) => void;
  /** Called after every successful open; `reconnect` is false for the first one. */
  onOpen?: (reconnect: boolean) => void;
  initialSeq?: number;
  flushMs?: number;
  minBackoffMs?: number;
  maxBackoffMs?: number;
  factory?: WsFactory;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (h: unknown) => void;
  random?: () => number;
}

export function isEvent(x: unknown): x is CLEvent {
  if (!x || typeof x !== "object") return false;
  const e = x as Partial<CLEvent>;
  return typeof e.seq === "number" && typeof e.type === "string";
}

/** Exponential backoff with +-20% jitter, capped. Exported for tests. */
export function backoffDelay(attempt: number, minMs: number, maxMs: number, random: () => number = Math.random): number {
  const base = Math.min(maxMs, minMs * 2 ** Math.max(0, attempt - 1));
  const jitter = base * 0.2 * (random() * 2 - 1);
  return Math.max(minMs, Math.min(maxMs, Math.round(base + jitter)));
}

export function eventsUrl(after: number, loc: { protocol: string; host: string } = window.location): string {
  const proto = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${loc.host}/ws/events?after=${Math.max(0, Math.floor(after))}`;
}

export class EventStream {
  private ws: WsLike | null = null;
  private stopped = true;
  private everOpened = false;
  private buffer: CLEvent[] = [];
  private flushTimer: unknown = null;
  private retryTimer: unknown = null;
  private firstMessageOfConnection = false;
  private connectAfter = 0;
  private readonly o: Required<Omit<EventStreamOptions, "onInfo" | "onOpen" | "initialSeq">> &
    Pick<EventStreamOptions, "onInfo" | "onOpen">;
  info: SocketInfo;

  constructor(opts: EventStreamOptions) {
    this.o = {
      flushMs: 40,
      minBackoffMs: 500,
      maxBackoffMs: 10_000,
      factory: (url) => new WebSocket(url) as unknown as WsLike,
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
      random: Math.random,
      ...opts,
    };
    this.info = {
      status: "idle",
      lastSeq: Math.max(0, opts.initialSeq ?? 0),
      attempts: 0,
      retryInMs: null,
      lastError: null,
      connectedAt: null,
    };
  }

  get lastSeq(): number {
    return this.info.lastSeq;
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.retryTimer) this.o.clearTimer(this.retryTimer);
    this.retryTimer = null;
    this.flush();
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      ws.onopen = null;
      try {
        ws.close(1000, "client stop");
      } catch {
        /* already closed */
      }
    }
    this.setInfo({ status: "idle", retryInMs: null });
  }

  /** Skip the remaining backoff and reconnect now (the "Retry" button). */
  retryNow(): void {
    if (this.stopped || this.info.status === "open" || this.info.status === "connecting") return;
    if (this.retryTimer) this.o.clearTimer(this.retryTimer);
    this.retryTimer = null;
    this.connect();
  }

  private setInfo(patch: Partial<SocketInfo>): void {
    this.info = { ...this.info, ...patch };
    this.o.onInfo?.(this.info);
  }

  private connect(): void {
    if (this.stopped) return;
    this.connectAfter = this.info.lastSeq;
    this.setInfo({ status: "connecting", attempts: this.info.attempts + 1, retryInMs: null });
    let ws: WsLike;
    try {
      ws = this.o.factory(this.o.url(this.connectAfter));
    } catch (e) {
      this.setInfo({ lastError: e instanceof Error ? e.message : String(e) });
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    this.firstMessageOfConnection = true;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      const reconnect = this.everOpened;
      this.everOpened = true;
      this.setInfo({ status: "open", attempts: 0, lastError: null, connectedAt: Date.now() });
      this.o.onOpen?.(reconnect);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      this.receive(ev.data);
    };
    ws.onerror = () => {
      if (this.ws !== ws) return;
      this.setInfo({ lastError: "WebSocket error" });
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.flush();
      this.setInfo({
        status: "closed",
        connectedAt: null,
        lastError: this.info.lastError ?? (ev && ev.code ? `closed (code ${ev.code})` : "closed"),
      });
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    const delay = backoffDelay(Math.max(1, this.info.attempts), this.o.minBackoffMs, this.o.maxBackoffMs, this.o.random);
    this.setInfo({ retryInMs: delay });
    this.retryTimer = this.o.setTimer(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  /** Parse one frame; exported behaviour is exercised through tests via the fake socket. */
  private receive(raw: unknown): void {
    if (typeof raw !== "string") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    const list = Array.isArray(parsed) ? parsed : [parsed];
    for (const item of list) {
      if (!isEvent(item)) continue;
      if (this.firstMessageOfConnection) {
        this.firstMessageOfConnection = false;
        // The server only replays seq > after. Anything lower on a fresh connection
        // means its counter restarted (database reset): start over from there.
        if (item.seq <= this.connectAfter && this.connectAfter > 0) {
          this.info = { ...this.info, lastSeq: 0 };
        }
      }
      if (item.seq <= this.info.lastSeq) continue; // duplicate
      this.info = { ...this.info, lastSeq: item.seq };
      this.buffer.push(item);
    }
    if (this.buffer.length && !this.flushTimer) {
      this.flushTimer = this.o.setTimer(() => this.flush(), this.o.flushMs);
    }
  }

  private flush(): void {
    if (this.flushTimer) this.o.clearTimer(this.flushTimer);
    this.flushTimer = null;
    if (!this.buffer.length) return;
    const batch = this.buffer;
    this.buffer = [];
    this.o.onEvents(batch);
    this.o.onInfo?.(this.info);
  }
}
