/**
 * First-run workspace: example projects that fill the real prompt (each one is
 * a phrasing the offline interpreter and the LLM both handle), how a build
 * works, and a readiness checklist built from GET /api/environment with
 * recovery guidance for anything missing.
 */

import type { ReactNode } from "react";
import { cx } from "../components/ui";
import { setUi } from "../lib/ui";
import { SecurityStart } from "../security/SecurityPanel";
import { useAppState } from "../store/store";

export const EXAMPLE_PROJECTS: Array<{ title: string; blurb: string; prompt: string; tag: string }> = [
  {
    title: "Binary counter",
    tag: "counter",
    blurb: "4-bit counter on LD0–LD3, 3× faster while BTNU is held.",
    prompt: "Build a 4-bit binary counter on LEDs 0-3 that counts once per second and three times faster while I hold BTNU.",
  },
  {
    title: "Traffic light controller",
    tag: "FSM",
    blurb: "Green, yellow, red state machine with exact durations.",
    prompt: "Make a traffic light on LEDs 0-2: green 4 seconds, yellow 1 second, red 3 seconds.",
  },
  {
    title: "Alternating LEDs",
    tag: "blinker",
    blurb: "LD0–LD7 alternate at 2 Hz, even against odd.",
    prompt: "Blink LEDs 0-7 alternately at 2 Hz.",
  },
  {
    title: "Button-controlled pattern",
    tag: "pattern",
    blurb: "Knight-rider sweep on LD0–LD7, double speed on BTNU.",
    prompt: "Knight rider pattern on LEDs 0-7 at 4 steps per second, twice as fast while I hold BTNU.",
  },
];

export function fillPrompt(text: string): void {
  window.dispatchEvent(new CustomEvent("circuitloop:fill-composer", { detail: text }));
}

export function Welcome() {
  return (
    <div className="mx-auto max-w-3xl space-y-8 px-2 py-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-fg">Describe a circuit. The design agent engineers it.</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted">
          The agent writes a hardware specification, generates Verilog, tests it in Verilator against scenarios derived
          from the spec, repairs failures, builds a bitstream with Vivado, and, after you approve, programs the Basys 3
          and checks the LEDs with your webcam.
        </p>
      </div>

      <section aria-label="Example projects">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-faint">Start from an example</h2>
        <div className="grid gap-2 sm:grid-cols-2">
          {EXAMPLE_PROJECTS.map((ex) => (
            <button key={ex.title} type="button" onClick={() => fillPrompt(ex.prompt)}
                    className="group rounded-xl border border-line bg-panel p-3.5 text-left shadow-panel transition-colors duration-150 hover:border-accent/50">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-fg">{ex.title}</span>
                <span className="rounded bg-raised px-1.5 py-0.5 font-mono text-2xs text-faint">{ex.tag}</span>
              </div>
              <p className="mt-1 text-xs text-muted">{ex.blurb}</p>
              <p className="mt-2 line-clamp-2 text-2xs text-faint group-hover:text-muted">“{ex.prompt}”</p>
              <span className="mt-2 inline-block text-2xs font-medium text-accent">Use this prompt →</span>
            </button>
          ))}
        </div>
      </section>

      <section aria-label="Hardware security audit">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-faint">Or audit hardware security (SiliconBreak X)</h2>
        <SecurityStart />
      </section>

      <section aria-label="How a build works">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-faint">What happens when you send it</h2>
        <ol className="grid gap-2 sm:grid-cols-4">
          {[
            ["Specify", "Request → explicit spec, checked against the board"],
            ["Verify", "Verilog + independent tests in Verilator, bounded auto-repair"],
            ["Build", "Vivado synthesis, place & route, timing, bitstream"],
            ["Prove", "Approved programming, then webcam measurement"],
          ].map(([t, d], i) => (
            <li key={t} className="rounded-lg border border-line p-3">
              <div className="font-mono text-2xs text-accent">0{i + 1}</div>
              <div className="mt-0.5 text-sm font-medium">{t}</div>
              <div className="mt-0.5 text-xs text-muted">{d}</div>
            </li>
          ))}
        </ol>
      </section>

      <Readiness />
    </div>
  );
}

function Readiness() {
  const env = useAppState((s) => s.environment);
  const hw = useAppState((s) => s.hardware);
  if (!env) return null;
  const viv = env.vivado;
  const items: Array<{ ok: boolean | null; label: string; detail: ReactNode }> = [
    { ok: env.verilator.available, label: "Verilator", detail: env.verilator.available ? env.verilator.version : "Install with: brew install verilator" },
    {
      ok: !!viv.ready, label: "Vivado (Docker)",
      detail: viv.ready ? `${viv.vivado_version} in ${viv.active_container}` : !viv.docker_daemon ? "Start Docker Desktop" :
        "No Vivado container running: start your usual container or approve a headless one from the top bar",
    },
    {
      ok: hw?.state === "connected" ? true : hw?.state === "no_board" ? false : null, label: "Basys 3",
      detail: hw?.state === "connected" ? "Detected" : hw?.state === "no_board" ? "Not connected: simulation and builds still work; plug in the board to program it" : hw?.message ?? "Checking…",
    },
    {
      ok: env.llm.configured, label: "AI model",
      detail: env.llm.configured ? `${env.llm.model}` : env.llm.key_present ? "Key present; live AI calls are off (Security tab → AI credits)" : (
        <span>Offline rules mode (no API key).{" "}
          <button type="button" className="text-accent hover:underline" onClick={() => setUi({ aiDialog: true })}>Set up the model</button></span>
      ),
    },
  ];
  return (
    <section aria-label="Readiness">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-faint">This machine</h2>
      <ul className="divide-y divide-line rounded-lg border border-line">
        {items.map((it) => (
          <li key={it.label} className="flex items-start gap-3 px-3 py-2 text-sm">
            <span className={cx("mt-1.5 h-2 w-2 flex-none rounded-full", it.ok === true ? "bg-ok" : it.ok === false ? "bg-warn" : "bg-idle")} />
            <span className="w-32 flex-none font-medium">{it.label}</span>
            <span className="min-w-0 text-xs text-muted">{it.detail}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
