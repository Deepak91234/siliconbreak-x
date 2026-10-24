/**
 * 3D is optional: when the browser cannot create a WebGL context (or the 3D view
 * throws), the inspector shows the 2D twin with a truthful notice instead of an error.
 */

import { Component, type ReactNode } from "react";

let cached: boolean | null = null;

export function webglAvailable(): boolean {
  if (cached !== null) return cached;
  try {
    const c = document.createElement("canvas");
    cached = !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch {
    cached = false;
  }
  return cached;
}

export function ThreeDUnavailable({ reason }: { reason: string }) {
  return (
    <p role="status" className="mb-2 rounded border border-warn/40 bg-warn/5 px-2 py-1.5 text-2xs text-warn">
      3D view unavailable ({reason}). Showing the 2D board, which uses the same pin mapping and states.
    </p>
  );
}

interface State { error: Error | null }

export class ThreeDBoundary extends Component<{ fallback: (error: Error) => ReactNode; children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error): void {
    console.warn("[3D board] falling back to 2D:", error.message);
  }

  render() {
    return this.state.error ? this.props.fallback(this.state.error) : this.props.children;
  }
}
