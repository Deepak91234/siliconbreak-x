/**
 * SVG waveform viewer for GET /api/revisions/{id}/waveform (vcd_to_json).
 *
 * Wheel zooms around the pointer, drag (or shift+wheel) pans, the cursor reads
 * out each signal's value. When zoomed in far, the visible window is
 * re-requested with start/end so the server's per-signal change cap does not
 * hide detail. Stimulus events and first-failure cycles from the simulation
 * report are drawn as markers.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement, type RefObject } from "react";
import { api, errorMessage } from "../api/client";
import type { SimulationReport, Waveform, WaveSignal } from "../api/types";
import { useResource } from "../lib/useResource";
import {
  clampView,
  defaultSignalOrder,
  formatValue,
  bitLevel,
  niceTicks,
  parseValue,
  segments,
  shortSignalName,
  ticksPerCycle,
  valueAt,
  type Radix,
} from "../lib/waveform";
import { Button, Empty, ErrorNote, Loading, Segmented, Select, Spinner, cx } from "../components/ui";

const LABEL_W = 200;
const ROW_H = 28;
const AXIS_H = 26;
const DEFAULT_SIGNALS = 16;

interface Marker {
  t: number; // in VCD time units
  label: string;
  tone: "info" | "bad";
}

/** Fill in the value at the window start from the overview when the detail fetch omits it. */
function mergeDetail(detail: Waveform, overview: Waveform): Waveform {
  const ov = new Map(overview.signals.map((s) => [s.name, s]));
  return {
    ...detail,
    signals: detail.signals.map((s) => {
      const first = s.changes[0];
      if (first && first[0] <= detail.start) return s;
      const base = ov.get(s.name);
      const v = base ? valueAt(base.changes, detail.start) : null;
      return v === null ? s : { ...s, changes: [[detail.start, v] as [number, string], ...s.changes] };
    }),
  };
}

function useWidth<T extends HTMLElement>(): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(800);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => setW(Math.max(320, Math.floor(entries[0].contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function SignalRow({ sig, y, x, v0, v1, plotW, radix }: {
  sig: WaveSignal;
  y: number;
  x: (t: number) => number;
  v0: number;
  v1: number;
  plotW: number;
  radix: Radix;
}) {
  const segs = segments(sig.changes, v0, v1);
  const top = y + 5;
  const bot = y + ROW_H - 5;
  const mid = (top + bot) / 2;
  const parts: ReactElement[] = [];

  if (sig.width <= 1) {
    let d = "";
    let dense: Array<[number, number]> = [];
    let prevLevel: 0 | 1 | null | undefined;
    for (const s of segs) {
      const xa = x(s.t0);
      const xb = x(s.t1);
      if (xb - xa < 1.2 && segs.length > plotW) {
        const last = dense[dense.length - 1];
        if (last && xa - last[1] < 1) last[1] = xb;
        else dense.push([xa, xb]);
        prevLevel = undefined;
        continue;
      }
      const lv = bitLevel(s.value);
      if (lv === null) {
        parts.push(<rect key={`u${xa}`} x={xa} y={top} width={Math.max(0.5, xb - xa)} height={bot - top} fill="url(#wf-unknown)" className="text-bad" />);
        prevLevel = undefined;
        continue;
      }
      const yy = lv ? top : bot;
      if (prevLevel === undefined || d === "") d += `M${xa},${yy}`;
      else d += `L${xa},${yy}`;
      d += `L${xb},${yy}`;
      prevLevel = lv;
    }
    if (d) parts.push(<path key="p" d={d} fill="none" stroke="rgb(var(--ok))" strokeWidth={1.4} />);
    dense.forEach(([a, b], i) =>
      parts.push(<rect key={`d${i}`} x={a} y={top} width={Math.max(1, b - a)} height={bot - top} fill="rgb(var(--ok) / 0.35)" />),
    );
    dense = [];
  } else {
    for (const s of segs) {
      const xa = x(s.t0);
      const xb = x(s.t1);
      const w = xb - xa;
      if (w < 0.5) continue;
      const k = Math.min(3, w / 3);
      const known = parseValue(s.value) !== null;
      const shape = `M${xa},${mid} L${xa + k},${top} L${xb - k},${top} L${xb},${mid} L${xb - k},${bot} L${xa + k},${bot} Z`;
      parts.push(
        <path
          key={`b${xa}`}
          d={shape}
          fill={known ? "rgb(var(--info) / 0.12)" : "url(#wf-unknown)"}
          stroke={known ? "rgb(var(--info))" : "rgb(var(--bad))"}
          strokeWidth={1}
        />,
      );
      if (w > 26) {
        const label = formatValue(s.value, sig.width, radix);
        const maxChars = Math.floor((w - 8) / 6.4);
        parts.push(
          <text key={`t${xa}`} x={xa + w / 2} y={mid + 3.5} textAnchor="middle" className="fill-fg font-mono" fontSize={10.5}>
            {label.length > maxChars ? label.slice(0, Math.max(1, maxChars - 1)) + "…" : label}
          </text>,
        );
      }
    }
  }
  return <g>{parts}</g>;
}

function WaveCanvas({ wave, shown, view, setView, radix, markers, bounds }: {
  wave: Waveform;
  shown: WaveSignal[];
  view: [number, number];
  setView: (v: [number, number]) => void;
  radix: Radix;
  markers: Marker[];
  bounds: [number, number];
}) {
  const [wrapRef, width] = useWidth<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const drag = useRef<{ x: number; v: [number, number] } | null>(null);
  const plotW = width - LABEL_W - 8;
  const [v0, v1] = view;
  const tpc = ticksPerCycle(wave.time_unit);
  const x = useCallback((t: number) => LABEL_W + ((t - v0) / Math.max(1e-9, v1 - v0)) * plotW, [v0, v1, plotW]);
  const tAt = useCallback((px: number) => v0 + ((px - LABEL_W) / plotW) * (v1 - v0), [v0, v1, plotW]);
  const height = AXIS_H + shown.length * ROW_H + 4;

  const viewRef = useRef(view);
  viewRef.current = view;
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const rect = el.getBoundingClientRect();
      const px = e.clientX - rect.left;
      if (px < LABEL_W) return;
      e.preventDefault();
      const [a, b] = viewRef.current;
      const span = b - a;
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        const d = ((e.shiftKey ? e.deltaY : e.deltaX) / plotW) * span;
        setView(clampView(a + d, b + d, bounds[0], bounds[1]));
        return;
      }
      const f = Math.exp(e.deltaY * 0.0015);
      const t = a + ((px - LABEL_W) / plotW) * span;
      setView(clampView(t - (t - a) * f, t + (b - t) * f, bounds[0], bounds[1]));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [plotW, bounds, setView]);

  const ticks = niceTicks(v0 / tpc, v1 / tpc, Math.max(3, Math.floor(plotW / 90)));

  return (
    <div ref={wrapRef} className="w-full select-none overflow-hidden rounded-lg border border-line bg-sunken">
      <svg
        ref={svgRef}
        width={width}
        height={height}
        className={cx("block", drag.current ? "cursor-grabbing" : "cursor-crosshair")}
        onMouseDown={(e) => {
          drag.current = { x: e.clientX, v: viewRef.current };
        }}
        onMouseMove={(e) => {
          const rect = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const px = e.clientX - rect.left;
          if (drag.current) {
            const dt = ((e.clientX - drag.current.x) / plotW) * (drag.current.v[1] - drag.current.v[0]);
            setView(clampView(drag.current.v[0] - dt, drag.current.v[1] - dt, bounds[0], bounds[1]));
          }
          setCursor(px >= LABEL_W ? tAt(px) : null);
        }}
        onMouseUp={() => (drag.current = null)}
        onMouseLeave={() => {
          drag.current = null;
          setCursor(null);
        }}
      >
        <defs>
          <pattern id="wf-unknown" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="rgb(var(--bad) / 0.08)" />
            <line x1="0" y1="0" x2="0" y2="6" stroke="rgb(var(--bad) / 0.6)" strokeWidth="1.5" />
          </pattern>
          <clipPath id="wf-plot">
            <rect x={LABEL_W} y={0} width={plotW} height={height} />
          </clipPath>
        </defs>
        <rect x={0} y={0} width={LABEL_W} height={height} className="fill-panel" />
        {/* axis */}
        <g clipPath="url(#wf-plot)">
          {ticks.map((c) => (
            <g key={c}>
              <line x1={x(c * tpc)} x2={x(c * tpc)} y1={AXIS_H - 6} y2={height} stroke="rgb(var(--line))" strokeWidth={1} />
              <text x={x(c * tpc) + 3} y={AXIS_H - 10} className="fill-muted font-mono" fontSize={10}>
                {c}
              </text>
            </g>
          ))}
          {markers.map((m, i) => (
            <g key={i}>
              <line x1={x(m.t)} x2={x(m.t)} y1={AXIS_H - 4} y2={height} stroke={m.tone === "bad" ? "rgb(var(--bad))" : "rgb(var(--violet) / 0.7)"} strokeWidth={1} strokeDasharray={m.tone === "bad" ? "" : "3 3"} />
              <title>{m.label}</title>
              <path d={`M${x(m.t) - 4},${AXIS_H - 6} L${x(m.t) + 4},${AXIS_H - 6} L${x(m.t)},${AXIS_H - 1} Z`} fill={m.tone === "bad" ? "rgb(var(--bad))" : "rgb(var(--violet))"} />
            </g>
          ))}
        </g>
        <text x={8} y={AXIS_H - 10} className="fill-faint" fontSize={10}>
          {tpc === 2 ? "clock cycles" : `time (${wave.timescale || wave.time_unit})`}
        </text>
        <line x1={0} x2={width} y1={AXIS_H} y2={AXIS_H} stroke="rgb(var(--line))" />
        {shown.map((sig, i) => {
          const y = AXIS_H + i * ROW_H;
          const cv = cursor !== null ? valueAt(sig.changes, cursor) : null;
          return (
            <g key={sig.name}>
              {i % 2 === 1 && <rect x={LABEL_W} y={y} width={plotW + 8} height={ROW_H} fill="rgb(var(--raised) / 0.35)" />}
              <text x={8} y={y + ROW_H / 2 + 3.5} className="fill-fg font-mono" fontSize={11}>
                <title>{sig.name}</title>
                {shortSignalName(sig.name).slice(-22)}
                {sig.width > 1 ? `[${sig.width - 1}:0]` : ""}
              </text>
              {cursor !== null && (
                <text x={LABEL_W - 6} y={y + ROW_H / 2 + 3.5} textAnchor="end" className="fill-accent font-mono" fontSize={10.5}>
                  {formatValue(cv, sig.width, radix)}
                </text>
              )}
              <g clipPath="url(#wf-plot)">
                <SignalRow sig={sig} y={y} x={x} v0={v0} v1={v1} plotW={plotW} radix={radix} />
              </g>
              <line x1={0} x2={width} y1={y + ROW_H} y2={y + ROW_H} stroke="rgb(var(--line) / 0.5)" />
            </g>
          );
        })}
        {cursor !== null && (
          <g pointerEvents="none">
            <line x1={x(cursor)} x2={x(cursor)} y1={AXIS_H} y2={height} stroke="rgb(var(--accent))" strokeWidth={1} />
            <rect x={x(cursor) + 4} y={2} width={92} height={14} rx={3} className="fill-accent/90" />
            <text x={x(cursor) + 8} y={12} fontSize={10} className="fill-slate-950 font-mono">
              cycle {(cursor / tpc).toFixed(tpc === 2 ? 1 : 0)}
            </text>
          </g>
        )}
        <line x1={LABEL_W} x2={LABEL_W} y1={0} y2={height} stroke="rgb(var(--line))" />
      </svg>
    </div>
  );
}

export function WaveformViewer({ revisionId, report, version, initialScenario }: {
  revisionId: string;
  report: SimulationReport;
  version: number;
  initialScenario?: string | null;
}) {
  const withVcd = report.scenarios.filter((s) => !!s.vcd_file);
  const firstFailing = withVcd.find((s) => !s.passed);
  const [scenario, setScenario] = useState<string>(initialScenario ?? firstFailing?.id ?? withVcd[0]?.id ?? "");
  useEffect(() => {
    if (initialScenario) setScenario(initialScenario);
  }, [initialScenario]);
  const [radix, setRadix] = useState<Radix>("hex");
  const [picked, setPicked] = useState<string[] | null>(null);
  const [showPicker, setShowPicker] = useState(false);

  const overview = useResource(scenario ? `${revisionId}:${scenario}` : null, version, (signal) => api.waveform(revisionId, { scenario }, signal));
  const wave = overview.data;
  const bounds: [number, number] = wave ? [wave.start, Math.max(wave.end, wave.start + 1)] : [0, 1];
  const [view, setViewRaw] = useState<[number, number]>([0, 1]);
  useEffect(() => {
    if (wave) setViewRaw([wave.start, Math.max(wave.end, wave.start + 1)]);
    setPicked(null);
  }, [wave]);
  const setView = useCallback((v: [number, number]) => setViewRaw(v), []);

  const ordered = useMemo(() => (wave ? defaultSignalOrder(wave.signals) : []), [wave]);
  const shownNames = picked ?? ordered.slice(0, DEFAULT_SIGNALS).map((s) => s.name);

  // Detail fetch for the visible window when zoomed in.
  const [detail, setDetail] = useState<{ key: string; start: number; end: number; data: Waveform } | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const dkey = `${revisionId}:${scenario}:${version}:${shownNames.join(",")}`;
  useEffect(() => {
    if (!wave) return;
    const full = bounds[1] - bounds[0];
    const span = view[1] - view[0];
    if (span >= full * 0.5) return;
    if (detail && detail.key === dkey && detail.start <= view[0] && detail.end >= view[1] && detail.end - detail.start <= span * 4) return;
    const ctrl = new AbortController();
    const h = setTimeout(() => {
      const start = Math.max(bounds[0], Math.floor(view[0] - span));
      const end = Math.min(bounds[1], Math.ceil(view[1] + span));
      setDetailBusy(true);
      api.waveform(revisionId, { scenario, start, end, signals: shownNames }, ctrl.signal).then(
        (data) => {
          if (ctrl.signal.aborted) return;
          setDetail({ key: dkey, start, end, data: mergeDetail(data, wave) });
          setDetailError(null);
          setDetailBusy(false);
        },
        (e) => {
          if (ctrl.signal.aborted) return;
          setDetailError(errorMessage(e));
          setDetailBusy(false);
        },
      );
    }, 250);
    return () => {
      clearTimeout(h);
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, wave, dkey]);

  const useDetail = detail && detail.key === dkey && detail.start <= view[0] && detail.end >= view[1] && view[1] - view[0] < (bounds[1] - bounds[0]) * 0.5;
  const source = useDetail ? detail!.data : wave;
  const shown = useMemo(() => {
    if (!source) return [];
    const byName = new Map(source.signals.map((s) => [s.name, s]));
    return shownNames.map((n) => byName.get(n)).filter((s): s is WaveSignal => !!s);
  }, [source, shownNames]);

  const tpc = ticksPerCycle(wave?.time_unit);
  const markers = useMemo<Marker[]>(() => {
    const out: Marker[] = [];
    const sc = report.plan?.scenarios.find((s) => s.id === scenario);
    for (const ev of sc?.events ?? []) out.push({ t: ev.cycle * tpc, label: `cycle ${ev.cycle}: ${ev.port} = ${ev.value}`, tone: "info" });
    const res = report.scenarios.find((s) => s.id === scenario);
    for (const c of res?.checks ?? []) {
      if (!c.passed && c.first_failure_cycle != null) out.push({ t: c.first_failure_cycle * tpc, label: `${c.id} first fails at cycle ${c.first_failure_cycle}: ${c.summary}`, tone: "bad" });
    }
    return out;
  }, [report, scenario, tpc]);

  if (!withVcd.length) {
    return <Empty title="No waveforms recorded">None of the scenarios in this simulation report has a VCD file.</Empty>;
  }

  const zoom = (f: number) => {
    const c = (view[0] + view[1]) / 2;
    const half = ((view[1] - view[0]) / 2) * f;
    setView(clampView(c - half, c + half, bounds[0], bounds[1]));
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Select value={scenario} onChange={setScenario}>
          {withVcd.map((s) => (
            <option key={s.id} value={s.id}>
              {s.passed ? "✓" : "✗"} {s.id} – {s.title}
            </option>
          ))}
        </Select>
        <Segmented value={radix} onChange={setRadix} options={[{ value: "hex", label: "hex" }, { value: "dec", label: "dec" }, { value: "bin", label: "bin" }]} />
        <div className="flex items-center gap-1">
          <Button size="sm" variant="secondary" onClick={() => zoom(0.5)} title="Zoom in">+</Button>
          <Button size="sm" variant="secondary" onClick={() => zoom(2)} title="Zoom out">&minus;</Button>
          <Button size="sm" variant="secondary" onClick={() => setView([bounds[0], bounds[1]])}>Fit</Button>
        </div>
        <Button size="sm" variant="ghost" onClick={() => setShowPicker(!showPicker)}>
          Signals ({shownNames.length}/{ordered.length})
        </Button>
        <span className="text-faint">
          cycles {(view[0] / tpc).toFixed(0)}&ndash;{(view[1] / tpc).toFixed(0)}
          {useDetail ? " · detail" : ""}
        </span>
        {(overview.loading || detailBusy) && <Spinner className="h-3.5 w-3.5 text-muted" />}
        <span className="ml-auto text-[10px] text-faint">wheel: zoom · drag / shift+wheel: pan</span>
      </div>
      {showPicker && wave && (
        <div className="rounded-md border border-line bg-panel p-2">
          <div className="mb-1 flex gap-2 text-[11px]">
            <button type="button" className="text-accent hover:underline" onClick={() => setPicked(ordered.map((s) => s.name))}>all</button>
            <button type="button" className="text-accent hover:underline" onClick={() => setPicked(ordered.filter((s) => !shortSignalName(s.name).includes(".")).map((s) => s.name))}>top-level ports</button>
            <button type="button" className="text-accent hover:underline" onClick={() => setPicked(null)}>default</button>
          </div>
          <div className="grid max-h-48 grid-cols-1 gap-x-4 overflow-auto sm:grid-cols-2 lg:grid-cols-3">
            {ordered.map((s) => (
              <label key={s.name} className="flex items-center gap-1.5 truncate font-mono text-[11px]" title={s.name}>
                <input
                  type="checkbox"
                  checked={shownNames.includes(s.name)}
                  onChange={(e) => setPicked(e.target.checked ? [...shownNames, s.name] : shownNames.filter((n) => n !== s.name))}
                />
                {shortSignalName(s.name)}
                {s.width > 1 ? `[${s.width - 1}:0]` : ""}
              </label>
            ))}
          </div>
        </div>
      )}
      {overview.error && !wave && (overview.notFound ? <Empty title="Waveform not available">The server has no VCD for this scenario.</Empty> : <ErrorNote error={overview.error} onRetry={overview.reload} />)}
      {detailError && <ErrorNote error={`Detail window failed: ${detailError}`} />}
      {overview.loading && !wave && <Loading label="Loading waveform…" />}
      {wave && shown.length === 0 && <Empty title="No signals selected" />}
      {wave && shown.length > 0 && <WaveCanvas wave={wave} shown={shown} view={view} setView={setView} radix={radix} markers={markers} bounds={bounds} />}
      {wave && (
        <div className="flex flex-wrap gap-3 text-[10px] text-faint">
          <span><span className="text-violet">&#9660;</span> stimulus event</span>
          <span><span className="text-bad">&#9660;</span> first failing cycle of a check</span>
          <span>hatched = unknown (x/z or before first sample)</span>
          <span>timescale {wave.timescale || "–"} · unit {wave.time_unit}</span>
        </div>
      )}
    </div>
  );
}
