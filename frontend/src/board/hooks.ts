/**
 * Board-state sources. Each returns LED values together with their provenance so
 * the views can never present a simulated value as an observation:
 *
 *  useReplay        replays a recorded Verilator scenario (stimulus + LED trace) in real time
 *  useVirtualFpga   re-simulates the revision's compiled RTL on the user's virtual inputs
 *  useObserved      LED states measured by an active webcam verification session
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { ScenarioTrace, VirtualEvent, VirtualResult } from "../api/types";
import { useAppState } from "../store/store";
import { inputPort } from "./model";

export function ledsFromValue(v: number | null | undefined): Record<number, boolean | null> {
  const out: Record<number, boolean | null> = {};
  for (let i = 0; i < 16; i++) out[i] = v == null ? null : ((v >> i) & 1) === 1;
  return out;
}

/** Value of a step function given as sorted [t, value] pairs. */
function valueAt(changes: Array<[number, number]>, t: number): number | null {
  let v: number | null = null;
  for (const [ct, cv] of changes) {
    if (ct > t) break;
    v = cv;
  }
  return v;
}

function useNow(active: boolean, intervalMs = 50): number {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(performance.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
  return now;
}

// ------------------------------------------------------------------ replay

export interface ReplayState {
  trace: ScenarioTrace | null;
  error: string | null;
  loading: boolean;
  playing: boolean;
  t: number;
  speed: number;
  leds: Record<number, boolean | null>;
  switches: Record<number, boolean>;
  buttons: Record<string, boolean>;
  play: () => void;
  pause: () => void;
  seek: (t: number) => void;
  setSpeed: (x: number) => void;
}

export function useReplay(revisionId: string | null, scenarioId: string | null): ReplayState {
  const [trace, setTrace] = useState<ScenarioTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [base, setBase] = useState<{ t0: number; at: number }>({ t0: 0, at: performance.now() });
  const now = useNow(playing);

  useEffect(() => {
    setTrace(null);
    setPlaying(false);
    setBase({ t0: 0, at: performance.now() });
    if (!revisionId || !scenarioId) return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api.scenarioTrace(revisionId, scenarioId, ctrl.signal).then(
      (tr) => {
        setTrace(tr);
        setLoading(false);
      },
      (e) => {
        if (!ctrl.signal.aborted) {
          setError(errorMessage(e));
          setLoading(false);
        }
      },
    );
    return () => ctrl.abort();
  }, [revisionId, scenarioId]);

  const duration = trace?.duration_s ?? 0;
  let t = playing ? base.t0 + ((now - base.at) / 1000) * speed : base.t0;
  if (t >= duration && duration > 0) t = duration;
  useEffect(() => {
    if (playing && duration > 0 && t >= duration) {
      setPlaying(false);
      setBase({ t0: duration, at: performance.now() });
    }
  }, [playing, t, duration]);

  const led = trace ? valueAt(trace.outputs.led?.map(([, v, s]) => [s, v] as [number, number]) ?? [], t) : null;
  const switches: Record<number, boolean> = {};
  const buttons: Record<string, boolean> = {};
  let swMask = 0;
  for (const e of trace?.inputs ?? []) {
    if (e.t_s > t) break;
    if (e.port === "sw") swMask = e.value;
    else buttons[e.port] = e.value === 1;
  }
  for (let i = 0; i < 16; i++) switches[i] = ((swMask >> i) & 1) === 1;

  return {
    trace, error, loading, playing, t, speed,
    leds: ledsFromValue(led),
    switches, buttons,
    play: () => {
      const start = t >= duration ? 0 : t;
      setBase({ t0: start, at: performance.now() });
      setPlaying(true);
    },
    pause: () => {
      setBase({ t0: t, at: performance.now() });
      setPlaying(false);
    },
    seek: (x: number) => setBase({ t0: Math.max(0, Math.min(duration, x)), at: performance.now() }),
    setSpeed: (x: number) => {
      setBase({ t0: t, at: performance.now() });
      setSpeed(x);
    },
  };
}

// ------------------------------------------------------------------ virtual FPGA

const LOOKAHEAD_S = 3;
const SESSION_CAP_S = 100;

export interface VirtualState {
  active: boolean;
  t: number;
  leds: Record<number, boolean | null>;
  switches: Record<number, boolean>;
  buttons: Record<string, boolean>;
  computing: boolean;
  error: string | null;
  events: number;
  result: VirtualResult | null;
  press: (id: string, down: boolean) => void;
  toggle: (id: string) => void;
  reset: () => void;
}

export function useVirtualFpga(revisionId: string | null, enabled: boolean): VirtualState {
  const [start, setStart] = useState(() => performance.now());
  const [events, setEvents] = useState<VirtualEvent[]>([]);
  const [swMask, setSwMask] = useState(0);
  const [buttons, setButtons] = useState<Record<string, boolean>>({});
  const [result, setResult] = useState<VirtualResult | null>(null);
  const [computing, setComputing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ctrlRef = useRef<AbortController | null>(null);
  const eventsRef = useRef<VirtualEvent[]>([]);
  eventsRef.current = events;
  const now = useNow(enabled && !!revisionId);
  const t = (now - start) / 1000;

  const reset = useCallback(() => {
    ctrlRef.current?.abort();
    setStart(performance.now());
    setEvents([]);
    setSwMask(0);
    setButtons({});
    setResult(null);
    setError(null);
  }, []);

  useEffect(() => {
    reset();
  }, [revisionId, enabled, reset]);

  const simulate = useCallback(async (horizon: number) => {
    if (!revisionId) return;
    ctrlRef.current?.abort();
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    setComputing(true);
    try {
      const r = await api.simulate(revisionId, eventsRef.current, Math.min(120, Math.max(0.5, horizon)), ctrl.signal);
      if (!ctrl.signal.aborted) {
        setResult(r);
        setError(null);
      }
    } catch (e) {
      if (!ctrl.signal.aborted) setError(errorMessage(e));
    } finally {
      if (!ctrl.signal.aborted) setComputing(false);
    }
  }, [revisionId]);

  // Re-simulate on every input change, and extend the lookahead before it runs out.
  useEffect(() => {
    if (!enabled || !revisionId) return;
    void simulate((performance.now() - start) / 1000 + LOOKAHEAD_S);
  }, [events, enabled, revisionId, simulate, start]);
  useEffect(() => {
    if (!enabled || !revisionId || computing || error) return; // after an error, retry only on new input
    if (t > SESSION_CAP_S) {
      reset();
      return;
    }
    if (!result || result.duration_s - t < 1.2) void simulate(t + LOOKAHEAD_S + 2);
  }, [t, enabled, revisionId, computing, result, simulate, reset]);

  const addEvent = (e: VirtualEvent) => setEvents((prev) => [...prev, e].slice(-480));

  const press = (id: string, down: boolean) => {
    const p = inputPort(id);
    if (!p || p.port === "sw" || !!buttons[id] === down) return;
    setButtons((b) => ({ ...b, [id]: down }));
    addEvent({ t_s: Math.max(0, (performance.now() - start) / 1000), port: p.port, value: down ? 1 : 0 });
  };
  const toggle = (id: string) => {
    const p = inputPort(id);
    if (!p || p.port !== "sw" || p.bit == null) return;
    const mask = swMask ^ (1 << p.bit);
    setSwMask(mask);
    addEvent({ t_s: Math.max(0, (performance.now() - start) / 1000), port: "sw", value: mask });
  };

  const fresh = result && t <= result.duration_s;
  const led = result && fresh ? valueAt(result.outputs.led ?? [], t) : null;
  const switches: Record<number, boolean> = {};
  for (let i = 0; i < 16; i++) switches[i] = ((swMask >> i) & 1) === 1;
  return {
    active: enabled && !!revisionId, t, leds: ledsFromValue(led), switches, buttons,
    computing: computing || !fresh, error, events: events.length, result, press, toggle, reset,
  };
}

// ------------------------------------------------------------------ observed (webcam)

export interface ObservedState {
  sessionId: string | null;
  status: string | null;
  leds: Record<number, boolean | null>;
  fps: number | null;
  result: string | null;
}

export function useObserved(revisionId: string | null): ObservedState {
  const sessions = useAppState((s) => s.vision);
  const list = Object.values(sessions).filter((v) => v.revision_id === revisionId);
  const sess = list.find((v) => v.status === "running") ?? list[list.length - 1] ?? null;
  const leds: Record<number, boolean | null> = {};
  for (let i = 0; i < 16; i++) leds[i] = null;
  const live = (sess?.live ?? {}) as { states?: Record<string, number | null>; fps?: number };
  for (const [k, v] of Object.entries(live.states ?? {})) leds[Number(k)] = v == null ? null : v === 1;
  return {
    sessionId: sess?.id ?? null, status: sess?.status ?? null, leds,
    fps: typeof live.fps === "number" ? live.fps : null,
    result: (sess?.result as { status?: string } | null)?.status ?? null,
  };
}
