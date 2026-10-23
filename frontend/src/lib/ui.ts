/**
 * Small client-only UI state shared across panels (not backend data):
 * the chat draft, which board components are highlighted (from the guide,
 * diagram or a hovered signal), board mode/view, inspector visibility and
 * presentation mode. Backend data stays in the main store.
 */

import { useSyncExternalStore } from "react";

export type BoardMode = "preview" | "simulation" | "live";
export type BoardView = "2d" | "3d";

export interface UiState {
  draft: string;
  draftVersion: number; // bumps when something outside the chat sets the draft (focus + replace)
  highlight: string[]; // component ids (btnU, sw3, led0, ...)
  highlightSource: string | null;
  boardMode: BoardMode;
  boardView: BoardView;
  inspectorOpen: boolean;
  presentation: boolean;
  aiDialog: boolean;
  inspectorDrawer: boolean; // narrow screens: inspector as a slide-over (not persisted)
}

const KEY = "circuitloop.ui";

function load(): Partial<UiState> {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Partial<UiState>) : {};
  } catch {
    return {};
  }
}

const saved = load();
let state: UiState = {
  draft: "",
  draftVersion: 0,
  highlight: [],
  highlightSource: null,
  boardMode: saved.boardMode === "preview" || saved.boardMode === "live" ? saved.boardMode : "simulation",
  boardView: saved.boardView === "3d" ? "3d" : "2d",
  inspectorOpen: saved.inspectorOpen !== false,
  presentation: false,
  aiDialog: false,
  inspectorDrawer: false,
};
const listeners = new Set<() => void>();

function persist(): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify({
      boardMode: state.boardMode, boardView: state.boardView, inspectorOpen: state.inspectorOpen,
    }));
  } catch {
    /* preference only */
  }
}

export function setUi(patch: Partial<UiState> | ((s: UiState) => Partial<UiState>)): void {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  persist();
  listeners.forEach((l) => l());
}

export function getUi(): UiState {
  return state;
}

export function useUi<T>(selector: (s: UiState) => T): T {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => selector(state),
  );
}

/** Put text into the chat composer (example cards, "try this" links). */
export function setDraft(text: string): void {
  setUi((s) => ({ draft: text, draftVersion: s.draftVersion + 1 }));
}

export function highlight(ids: string[], source: string | null = null): void {
  setUi({ highlight: ids, highlightSource: source });
}

/** Hardware toggle: docks/undocks on wide screens, opens/closes the slide-over on narrow ones. */
export const INSPECTOR_DOCK_MIN_PX = 1280;
export function toggleInspector(): void {
  if (window.innerWidth >= INSPECTOR_DOCK_MIN_PX) setUi((s) => ({ inspectorOpen: !s.inspectorOpen }));
  else setUi((s) => ({ inspectorDrawer: !s.inspectorDrawer }));
}
