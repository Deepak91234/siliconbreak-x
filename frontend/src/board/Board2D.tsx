/**
 * Top-down SVG Basys 3. Pure view: everything it shows comes from props
 * (board model + visual state + guide components). Inputs are interactive only
 * when `visual.interactive` (virtual FPGA mode): buttons are momentary
 * (pointer down/up, Space/Enter), switches toggle on click.
 */

import { useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { GuideComponent } from "../api/types";
import { cx } from "../components/ui";
import { BOARD_H, BOARD_W, PROVENANCE_LABEL, type BoardComponent, type BoardVisualState } from "./model";

interface Props {
  model: BoardComponent[];
  visual: BoardVisualState;
  guide: Map<string, GuideComponent>;
  onPress?: (id: string, down: boolean) => void;
  onToggle?: (id: string) => void;
  onHover?: (id: string | null) => void;
  className?: string;
}

interface Tip { id: string; x: number; y: number }

export function Board2D({ model, visual, guide, onPress, onToggle, onHover, className }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<Tip | null>(null);

  const showTip = (id: string, e: PointerEvent | { clientX: number; clientY: number }) => {
    const r = wrap.current?.getBoundingClientRect();
    if (!r) return;
    setTip({ id, x: e.clientX - r.left, y: e.clientY - r.top });
    onHover?.(id);
  };
  const hideTip = () => {
    setTip(null);
    onHover?.(null);
  };

  const interactive = (c: BoardComponent) => visual.interactive && (c.kind === "switch" || c.kind === "button");

  const keyDown = (c: BoardComponent, e: KeyboardEvent) => {
    if (!interactive(c) || (e.key !== " " && e.key !== "Enter")) return;
    e.preventDefault();
    if (c.kind === "switch") onToggle?.(c.id);
    else if (!e.repeat) onPress?.(c.id, true);
  };
  const keyUp = (c: BoardComponent, e: KeyboardEvent) => {
    if (interactive(c) && c.kind === "button" && (e.key === " " || e.key === "Enter")) onPress?.(c.id, false);
  };

  const tipComp = tip ? model.find((c) => c.id === tip.id) : null;
  const tipGuide = tip ? guide.get(tip.id) : undefined;

  return (
    <div ref={wrap} className={cx("relative w-full select-none", className)}>
      <svg viewBox={`-2 -2 ${BOARD_W + 4} ${BOARD_H + 4}`} className="block h-auto w-full" role="group"
           aria-label="Basys 3 board">
        <defs>
          <filter id="ledGlow" x="-200%" y="-200%" width="500%" height="500%">
            <feGaussianBlur stdDeviation="1.4" result="b" />
            <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <pattern id="pcbGrid" width="4" height="4" patternUnits="userSpaceOnUse">
            <path d="M4 0H0V4" fill="none" stroke="rgb(var(--board-line))" strokeWidth="0.08" />
          </pattern>
        </defs>
        <rect x="0" y="0" width={BOARD_W} height={BOARD_H} rx="3" className="fill-[rgb(var(--board))]" />
        <rect x="0" y="0" width={BOARD_W} height={BOARD_H} rx="3" fill="url(#pcbGrid)" />
        <rect x="0.4" y="0.4" width={BOARD_W - 0.8} height={BOARD_H - 0.8} rx="2.6" fill="none"
              stroke="rgb(var(--board-line))" strokeWidth="0.4" />
        {[[3, 3], [BOARD_W - 3, 3], [3, BOARD_H - 3], [BOARD_W - 3, BOARD_H - 3]].map(([x, y]) => (
          <circle key={`${x}-${y}`} cx={x} cy={y} r="1.3" className="fill-[rgb(var(--bg))]" stroke="rgb(var(--board-line))" strokeWidth="0.3" />
        ))}
        <text x={BOARD_W / 2} y="39.8" textAnchor="middle" className="fill-[rgb(var(--silk))]" fontSize="2" letterSpacing="0.4" opacity="0.8">
          DIGILENT BASYS 3
        </text>

        {model.map((c) => {
          const used = visual.used.has(c.id);
          const focus = visual.focus.has(c.id);
          const common = {
            onPointerEnter: (e: PointerEvent) => showTip(c.id, e),
            onPointerMove: (e: PointerEvent) => showTip(c.id, e),
            onPointerLeave: () => {
              hideTip();
              if (interactive(c) && c.kind === "button" && visual.buttons[c.id]) onPress?.(c.id, false);
            },
          };
          const ring = (pad: number) =>
            used || focus ? (
              <rect x={c.x - c.w / 2 - pad} y={c.y - c.h / 2 - pad} width={c.w + 2 * pad} height={c.h + 2 * pad} rx={pad + 0.6}
                    fill="none" strokeWidth={focus ? 0.7 : 0.4}
                    className={cx(focus ? "stroke-[rgb(var(--accent))] board-focus" : "stroke-[rgb(var(--accent)/0.55)]")} />
            ) : null;

          if (c.kind === "led") {
            const on = visual.leds[c.index ?? -1];
            const lit = on === true;
            return (
              <g key={c.id} {...common} aria-label={`${c.label}${on == null ? "" : lit ? " on" : " off"}`}>
                {ring(1.2)}
                {lit && <rect x={c.x - c.w / 2} y={c.y - c.h / 2} width={c.w} height={c.h} rx="0.4" fill="rgb(var(--led-on))" filter="url(#ledGlow)" opacity="0.9" />}
                <rect x={c.x - c.w / 2} y={c.y - c.h / 2} width={c.w} height={c.h} rx="0.4"
                      fill={lit ? "rgb(var(--led-on))" : "rgb(var(--led-off))"}
                      stroke={on == null && used ? "rgb(var(--muted))" : "none"} strokeDasharray={on == null && used ? "0.5 0.4" : undefined}
                      strokeWidth="0.25" />
                <text x={c.x} y={c.y + 3.6} textAnchor="middle" fontSize="1.7"
                      className={used ? "fill-[rgb(var(--fg))]" : "fill-[rgb(var(--silk))]"}>{c.label}</text>
              </g>
            );
          }
          if (c.kind === "switch") {
            const on = !!visual.switches[c.index ?? -1];
            const clickable = interactive(c);
            return (
              <g key={c.id} {...common} role={clickable ? "switch" : undefined} aria-checked={clickable ? on : undefined}
                 aria-label={`${c.label} ${on ? "on" : "off"}`} tabIndex={clickable ? 0 : undefined}
                 className={clickable ? "cursor-pointer focus:outline-none" : undefined}
                 onClick={() => clickable && onToggle?.(c.id)} onKeyDown={(e) => keyDown(c, e)}>
                {ring(0.9)}
                <rect x={c.x - c.w / 2} y={c.y - c.h / 2} width={c.w} height={c.h} rx="0.5" fill="rgb(var(--part))"
                      stroke="rgb(var(--part-edge))" strokeWidth="0.2" />
                <rect x={c.x - 0.9} y={on ? c.y - c.h / 2 + 0.6 : c.y + 0.4} width="1.8" height={c.h / 2 - 1}
                      rx="0.3" fill={on ? "rgb(var(--accent))" : "rgb(var(--lever))"} />
                <text x={c.x} y={c.y + c.h / 2 + 2.4} textAnchor="middle" fontSize="1.7"
                      className={used ? "fill-[rgb(var(--fg))]" : "fill-[rgb(var(--silk))]"}>{c.label}</text>
              </g>
            );
          }
          if (c.kind === "button") {
            const down = !!visual.buttons[c.id];
            const clickable = interactive(c);
            return (
              <g key={c.id} {...common} role={clickable ? "button" : undefined} aria-pressed={clickable ? down : undefined}
                 aria-label={`${c.label}${down ? " pressed" : ""}`} tabIndex={clickable ? 0 : undefined}
                 className={clickable ? "cursor-pointer focus:outline-none" : undefined}
                 onPointerDown={(e) => {
                   if (!clickable) return;
                   (e.target as Element).setPointerCapture?.(e.pointerId);
                   onPress?.(c.id, true);
                 }}
                 onPointerUp={() => clickable && onPress?.(c.id, false)}
                 onKeyDown={(e) => keyDown(c, e)} onKeyUp={(e) => keyUp(c, e)}>
                {ring(0.9)}
                <rect x={c.x - c.w / 2} y={c.y - c.h / 2} width={c.w} height={c.h} rx="0.6" fill="rgb(var(--part))"
                      stroke="rgb(var(--part-edge))" strokeWidth="0.2" />
                <circle cx={c.x} cy={c.y} r={down ? 1.6 : 1.9} fill={down ? "rgb(var(--accent))" : "rgb(var(--lever))"} />
                <text x={c.x} y={c.y + c.h / 2 + 2.2} textAnchor="middle" fontSize="1.6"
                      className={used ? "fill-[rgb(var(--fg))]" : "fill-[rgb(var(--silk))]"}>{c.label}</text>
              </g>
            );
          }
          if (c.kind === "seg") {
            return (
              <g key={c.id} {...common}>
                {ring(0.8)}
                <rect x={c.x - c.w / 2} y={c.y - c.h / 2} width={c.w} height={c.h} rx="0.8" fill="rgb(var(--part))" />
                {[0, 1, 2, 3].map((d) => <SevenSegDigit key={d} x={c.x - c.w / 2 + 2 + d * 7} y={c.y - c.h / 2 + 1.5} />)}
                <text x={c.x} y={c.y + c.h / 2 + 2.2} textAnchor="middle" fontSize="1.6" className="fill-[rgb(var(--silk))]">AN3 … AN0</text>
              </g>
            );
          }
          if (c.kind === "fpga") {
            return (
              <g key={c.id} {...common}>
                <rect x={c.x - c.w / 2} y={c.y - c.h / 2} width={c.w} height={c.h} rx="0.8" fill="rgb(var(--chip))"
                      stroke="rgb(var(--part-edge))" strokeWidth="0.3" />
                <text x={c.x} y={c.y - 1} textAnchor="middle" fontSize="1.7" className="fill-[rgb(var(--silk))]">XILINX</text>
                <text x={c.x} y={c.y + 1.6} textAnchor="middle" fontSize="1.5" className="fill-[rgb(var(--silk))]">ARTIX-7</text>
                <text x={c.x} y={c.y + 3.8} textAnchor="middle" fontSize="1.1" className="fill-[rgb(var(--silk))]">XC7A35T</text>
              </g>
            );
          }
          return (
            <g key={c.id} {...common}>
              <rect x={c.x - c.w / 2} y={c.y - c.h / 2} width={c.w} height={c.h} rx="0.5" fill="rgb(var(--part))" opacity="0.85" />
              <text x={c.x} y={c.y + 0.6} textAnchor="middle" fontSize="1.5" className="fill-[rgb(var(--silk))]">{c.label}</text>
            </g>
          );
        })}
      </svg>

      {tip && tipComp && (
        <div role="tooltip"
             className="pointer-events-none absolute z-20 w-64 -translate-x-1/2 rounded-md border border-line bg-raised/95 p-2.5 text-xs shadow-xl backdrop-blur"
             style={{ left: Math.min(Math.max(tip.x, 130), (wrap.current?.clientWidth ?? 300) - 130), top: tip.y + 16 }}>
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold text-fg">{tipComp.label}</span>
            {tipComp.pin && <span className="font-mono text-faint">pin {tipComp.pin}</span>}
          </div>
          {tipGuide ? (
            <>
              <div className="mt-1 font-mono text-accent">{tipGuide.signal}</div>
              <div className="mt-0.5 text-muted">{tipGuide.function}</div>
              {tipGuide.debounce_ms ? <div className="mt-0.5 text-faint">synchronized, debounced {tipGuide.debounce_ms} ms</div> : null}
            </>
          ) : (
            <div className="mt-1 text-faint">{tipComp.kind === "led" ? "Not used: held off by the trusted wrapper" :
              tipComp.kind === "seg" ? "Blanked by the trusted wrapper (not in the MVP spec)" :
                tipComp.kind === "switch" || tipComp.kind === "button" ? "Not used by this design" : "Board component"}</div>
          )}
          {tipComp.kind === "led" && visual.leds[tipComp.index ?? -1] != null && visual.ledSource && (
            <div className="mt-1.5 border-t border-line pt-1 text-faint">
              {visual.leds[tipComp.index ?? -1] ? "on" : "off"} · {PROVENANCE_LABEL[visual.ledSource]}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SevenSegDigit({ x, y }: { x: number; y: number }) {
  // Blank digit outline: the trusted wrapper drives an = 1111 (all digits off).
  const s = "rgb(var(--led-off))";
  return (
    <g>
      <rect x={x + 1} y={y} width="3" height="0.7" rx="0.3" fill={s} />
      <rect x={x + 1} y={y + 3.6} width="3" height="0.7" rx="0.3" fill={s} />
      <rect x={x + 1} y={y + 7.2} width="3" height="0.7" rx="0.3" fill={s} />
      <rect x={x + 0.2} y={y + 0.6} width="0.7" height="3" rx="0.3" fill={s} />
      <rect x={x + 4.1} y={y + 0.6} width="0.7" height="3" rx="0.3" fill={s} />
      <rect x={x + 0.2} y={y + 4.2} width="0.7" height="3" rx="0.3" fill={s} />
      <rect x={x + 4.1} y={y + 4.2} width="0.7" height="3" rx="0.3" fill={s} />
    </g>
  );
}
