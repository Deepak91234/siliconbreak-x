/**
 * Rotatable / zoomable 3D Basys 3 built from the same board model, visual state
 * and guide data as Board2D (1 unit = 1 mm; board top-left at (-W/2, 0, -H/2)).
 * Loaded lazily so three.js is only downloaded when the 3D view is opened.
 */

import { Edges, Html, OrbitControls } from "@react-three/drei";
import { Canvas, type ThreeEvent } from "@react-three/fiber";
import { useMemo, useState } from "react";
import type { GuideComponent } from "../api/types";
import { BOARD_H, BOARD_W, PROVENANCE_LABEL, type BoardComponent, type BoardVisualState } from "./model";

interface Props {
  model: BoardComponent[];
  visual: BoardVisualState;
  guide: Map<string, GuideComponent>;
  onPress?: (id: string, down: boolean) => void;
  onToggle?: (id: string) => void;
  onHover?: (id: string | null) => void;
}

const PCB_T = 1.6;

function cssColor(name: string, fallback: string): string {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
    if (v) return `rgb(${v.split(/\s+/).join(",")})`;
  } catch {
    /* SSR / tests */
  }
  return fallback;
}

function pos(c: BoardComponent, y: number): [number, number, number] {
  return [c.x - BOARD_W / 2, y, c.y - BOARD_H / 2];
}

export default function Board3D({ model, visual, guide, onPress, onToggle, onHover }: Props) {
  const [hover, setHover] = useState<string | null>(null);
  const colors = useMemo(() => ({
    board: cssColor("board", "rgb(14,52,47)"),
    part: cssColor("part", "rgb(26,31,36)"),
    lever: cssColor("lever", "rgb(178,188,198)"),
    chip: cssColor("chip", "rgb(18,21,24)"),
    accent: cssColor("accent", "rgb(45,212,191)"),
    ledOn: cssColor("led-on", "rgb(74,222,128)"),
    ledOff: cssColor("led-off", "rgb(30,52,42)"),
    unknown: cssColor("muted", "rgb(150,162,174)"),
  }), []);

  const enter = (id: string) => (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    setHover(id);
    onHover?.(id);
  };
  const leave = (id: string) => (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    setHover((h) => (h === id ? null : h));
    onHover?.(null);
    if (visual.interactive && visual.buttons[id]) onPress?.(id, false);
  };
  const interactive = (c: BoardComponent) => visual.interactive && (c.kind === "switch" || c.kind === "button");
  const hovered = hover ? model.find((c) => c.id === hover) : null;
  const hoveredGuide = hover ? guide.get(hover) : undefined;

  return (
    <div className="relative h-full min-h-[18rem] w-full overflow-hidden rounded-md bg-sunken">
      <Canvas camera={{ position: [0, 118, 104], fov: 38, near: 1, far: 1000 }} dpr={[1, 2]}
              gl={{ preserveDrawingBuffer: true, antialias: true }}
              onPointerMissed={() => setHover(null)} aria-label="3D Basys 3 board">
        <color attach="background" args={[cssColor("sunken", "rgb(6,8,11)")]} />
        <ambientLight intensity={0.9} />
        <directionalLight position={[40, 120, 60]} intensity={1.6} />
        <directionalLight position={[-60, 40, -40]} intensity={0.35} />

        <mesh position={[0, -PCB_T / 2, 0]} receiveShadow>
          <boxGeometry args={[BOARD_W, PCB_T, BOARD_H]} />
          <meshStandardMaterial color={colors.board} roughness={0.8} />
        </mesh>

        {model.map((c) => {
          const used = visual.used.has(c.id);
          const focus = visual.focus.has(c.id);
          const outline = used || focus ? <Edges scale={1.15} threshold={15} color={colors.accent} /> : null;
          const handlers = {
            onPointerOver: enter(c.id),
            onPointerOut: leave(c.id),
          };
          if (c.kind === "led") {
            const on = visual.leds[c.index ?? -1];
            return (
              <mesh key={c.id} position={pos(c, c.height3d / 2)} {...handlers}>
                <boxGeometry args={[c.w, c.height3d, c.h]} />
                <meshStandardMaterial color={on ? colors.ledOn : on == null && used ? colors.unknown : colors.ledOff}
                                      emissive={on ? colors.ledOn : "#000000"} emissiveIntensity={on ? 2.2 : 0} />
                {outline}
                {on && <pointLight color={colors.ledOn} intensity={4} distance={9} position={[0, 2, 0]} />}
              </mesh>
            );
          }
          if (c.kind === "switch") {
            const on = !!visual.switches[c.index ?? -1];
            return (
              <group key={c.id} position={pos(c, 0)}>
                <mesh position={[0, 1.2, 0]} {...handlers}
                      onClick={(e) => { e.stopPropagation(); if (interactive(c)) onToggle?.(c.id); }}>
                  <boxGeometry args={[c.w, 2.4, c.h]} />
                  <meshStandardMaterial color={colors.part} />
                  {outline}
                </mesh>
                <mesh position={[0, 3.0, on ? -c.h / 4 : c.h / 4]}>
                  <boxGeometry args={[1.6, 1.6, c.h / 2 - 0.6]} />
                  <meshStandardMaterial color={on ? colors.accent : colors.lever} />
                </mesh>
              </group>
            );
          }
          if (c.kind === "button") {
            const down = !!visual.buttons[c.id];
            return (
              <group key={c.id} position={pos(c, 0)}>
                <mesh position={[0, 1.1, 0]} {...handlers}
                      onPointerDown={(e) => { e.stopPropagation(); if (interactive(c)) onPress?.(c.id, true); }}
                      onPointerUp={(e) => { e.stopPropagation(); if (interactive(c)) onPress?.(c.id, false); }}>
                  <boxGeometry args={[c.w, 2.2, c.h]} />
                  <meshStandardMaterial color={colors.part} />
                  {outline}
                </mesh>
                <mesh position={[0, down ? 2.5 : 3.2, 0]}>
                  <cylinderGeometry args={[1.9, 1.9, 1.4, 24]} />
                  <meshStandardMaterial color={down ? colors.accent : colors.lever} />
                </mesh>
              </group>
            );
          }
          const color = c.kind === "fpga" ? colors.chip : colors.part;
          return (
            <mesh key={c.id} position={pos(c, c.height3d / 2)} {...handlers}>
              <boxGeometry args={[c.w, c.height3d, c.h]} />
              <meshStandardMaterial color={color} roughness={0.6} />
              {c.kind === "seg" && outline}
            </mesh>
          );
        })}

        {model.filter((c) => visual.focus.has(c.id) && c.kind !== "led").map((c) => (
          <Html key={`lbl-${c.id}`} position={pos(c, c.height3d + 4)} center distanceFactor={70} zIndexRange={[10, 0]}>
            <span className="whitespace-nowrap rounded bg-accent px-1.5 py-0.5 text-[10px] font-semibold text-accent-fg shadow">{c.label}</span>
          </Html>
        ))}

        {hovered && (
          <Html position={pos(hovered, hovered.height3d + 7)} center zIndexRange={[20, 10]}>
            <div className="pointer-events-none w-56 rounded-md border border-line bg-raised/95 p-2 text-xs text-fg shadow-pop">
              <div className="flex justify-between gap-2"><b>{hovered.label}</b>{hovered.pin && <span className="font-mono text-faint">pin {hovered.pin}</span>}</div>
              {hoveredGuide ? (
                <>
                  <div className="mt-0.5 font-mono text-accent">{hoveredGuide.signal}</div>
                  <div className="text-muted">{hoveredGuide.function}</div>
                </>
              ) : <div className="text-faint">Not used by this design</div>}
              {hovered.kind === "led" && visual.leds[hovered.index ?? -1] != null && visual.ledSource && (
                <div className="mt-1 border-t border-line pt-1 text-faint">
                  {visual.leds[hovered.index ?? -1] ? "on" : "off"} · {PROVENANCE_LABEL[visual.ledSource]}
                </div>
              )}
            </div>
          </Html>
        )}

        <OrbitControls makeDefault enablePan target={[0, 0, 2]} minDistance={40} maxDistance={260}
                       maxPolarAngle={Math.PI / 2.1} />
      </Canvas>
      <div className="pointer-events-none absolute bottom-2 left-2 rounded bg-panel/80 px-2 py-1 text-2xs text-faint">
        drag to rotate · scroll to zoom · right-drag to pan
      </div>
    </div>
  );
}
