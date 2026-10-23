/** Drag- and keyboard-resizable panel splitter (pointer drag or ←/→ keys when focused). */

import { useRef, type KeyboardEvent, type PointerEvent } from "react";

export function Splitter({ onResize, label, invert = false }: { onResize: (dx: number) => void; label: string; invert?: boolean }) {
  const last = useRef<number | null>(null);
  const down = (e: PointerEvent<HTMLDivElement>) => {
    last.current = e.clientX;
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const move = (e: PointerEvent<HTMLDivElement>) => {
    if (last.current == null) return;
    const dx = e.clientX - last.current;
    last.current = e.clientX;
    onResize(invert ? -dx : dx);
  };
  const up = () => {
    last.current = null;
  };
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      const d = (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 48 : 16);
      onResize(invert ? -d : d);
    }
  };
  return (
    <div role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0}
         onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onKeyDown={key}
         className="splitter group relative z-10 w-1.5 flex-none cursor-col-resize">
      <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-line transition-colors group-hover:bg-accent/60" />
    </div>
  );
}
