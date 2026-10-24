/**
 * Compact waveform for one verifier test window (GET /api/security/audits/{id}/waveform).
 * Times are VCD units of the trusted testbench (1 ns; clock period 10 ns).
 */

import { useMemo } from "react";
import { securityApi } from "../api/security";
import type { WaveSignal } from "../api/types";
import { ErrorNote, Loading } from "../components/ui";
import { useResource } from "../lib/useResource";
import { segments } from "../lib/waveform";

const LABEL_W = 110;
const ROW_H = 22;

function short(name: string): string {
  const parts = name.split(".");
  return parts[parts.length - 1];
}

function fmt(v: string | null, width: number): string {
  if (v == null) return "";
  if (v === "x" || v === "z") return v;
  const n = parseInt(v, 16);
  if (Number.isNaN(n)) return v;
  return width === 1 ? String(n) : `0x${n.toString(16).toUpperCase().padStart(Math.ceil(width / 4), "0")}`;
}

export function SecurityWave({ auditId, vcdPath, span, signals, highlight }: {
  auditId: string; vcdPath: string; span: [number, number]; signals: string[]; highlight?: string[];
}) {
  const [t0, t1] = span;
  const key = `${auditId}:${vcdPath}:${t0}:${t1}:${signals.join(",")}`;
  const res = useResource(key, 0, (sig) => securityApi.waveform(auditId, vcdPath, { start: t0, end: t1, signals }, sig));
  const order = useMemo(() => {
    const sigs = res.data?.signals ?? [];
    const by = new Map(sigs.map((s) => [short(s.name), s]));
    return signals.map((n) => by.get(short(n))).filter(Boolean) as WaveSignal[];
  }, [res.data, signals]);
  if (res.error) return <ErrorNote error={res.error} onRetry={res.reload} />;
  if (!res.data) return <Loading label="Loading waveform…" />;
  const W = 760;
  const plotW = W - LABEL_W - 8;
  const x = (t: number) => LABEL_W + ((t - t0) / Math.max(1, t1 - t0)) * plotW;
  const H = order.length * ROW_H + 18;
  const edges: number[] = [];
  for (let t = Math.ceil((t0 - 5) / 10) * 10 + 5; t <= t1; t += 10) edges.push(t);
  return (
    <div className="overflow-x-auto rounded border border-line bg-sunken">
      <svg viewBox={`0 0 ${W} ${H}`} className="min-w-[560px]" role="img" aria-label="Waveform of the selected test window">
        {edges.map((t) => (
          <line key={t} x1={x(t)} x2={x(t)} y1={0} y2={H - 14} className="stroke-line" strokeWidth={0.6} strokeDasharray="2 3" />
        ))}
        {order.map((s, i) => {
          const y = i * ROW_H + 4;
          const hl = highlight?.includes(short(s.name));
          const segs = segments(s.changes, t0, t1);
          return (
            <g key={s.name}>
              <text x={6} y={y + 13} className={hl ? "fill-accent" : "fill-muted"} fontSize={11} fontFamily="ui-monospace, monospace">
                {short(s.name)}
              </text>
              {segs.map((g, k) => {
                const xa = x(g.t0);
                const xb = x(g.t1);
                if (s.width === 1) {
                  const hi = g.value != null && g.value !== "x" && parseInt(g.value, 16) === 1;
                  const yy = hi ? y + 2 : y + 16;
                  const next = segs[k + 1];
                  const nextHi = next && next.value != null && next.value !== "x" && parseInt(next.value, 16) === 1;
                  return (
                    <g key={k}>
                      <line x1={xa} x2={xb} y1={yy} y2={yy} className={hl ? "stroke-accent" : "stroke-ok"} strokeWidth={1.4} />
                      {next && nextHi !== hi && <line x1={xb} x2={xb} y1={y + 2} y2={y + 16} className={hl ? "stroke-accent" : "stroke-ok"} strokeWidth={1.4} />}
                    </g>
                  );
                }
                return (
                  <g key={k}>
                    <path d={`M${xa + 2},${y + 2} L${xb - 2},${y + 2} L${xb},${y + 9} L${xb - 2},${y + 16} L${xa + 2},${y + 16} L${xa},${y + 9} Z`}
                          className={hl ? "fill-accent/10 stroke-accent" : "fill-info/10 stroke-info"} strokeWidth={1} />
                    {xb - xa > 26 && (
                      <text x={(xa + xb) / 2} y={y + 13} textAnchor="middle" fontSize={10} fontFamily="ui-monospace, monospace"
                            className="fill-fg">{fmt(g.value, s.width)}</text>
                    )}
                  </g>
                );
              })}
            </g>
          );
        })}
        <text x={LABEL_W} y={H - 3} fontSize={9} className="fill-faint">{t0} ns</text>
        <text x={W - 8} y={H - 3} fontSize={9} textAnchor="end" className="fill-faint">{t1} ns</text>
      </svg>
    </div>
  );
}
