/**
 * Board-level simulation table of a security case's Basys 3 demo: every demo step
 * on the full Basys 3 top (Verilator), compared with the LED pattern the contract
 * requires. Simulation only; shown under FPGA Lab → Technical details.
 */

import type { BoardSimStep } from "../api/security";
import { Pill, cx } from "../components/ui";

function Leds({ hex }: { hex: string }) {
  const v = parseInt(hex, 16);
  return (
    <span className="inline-flex gap-[3px]" aria-label={`LEDs ${hex}`} title={hex}>
      {Array.from({ length: 16 }, (_, k) => 15 - k).map((i) => (
        <span key={i} className={cx("h-2 w-1.5 rounded-sm", (v >> i) & 1 ? (i >= 14 ? (i === 15 ? "bg-ok" : "bg-bad") : "bg-warn") : "bg-line")} />
      ))}
    </span>
  );
}


export function BoardSim({ steps, note, message }: { steps: BoardSimStep[]; note: string; message: string }) {
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-2xs font-semibold uppercase tracking-wide text-faint">Demo steps · board-level simulation</span>
        <span className="text-2xs text-muted">{message}</span>
      </div>
      <table className="w-full text-2xs">
        <thead className="text-faint">
          <tr><th className="py-0.5 text-left font-normal">step</th><th className="text-left font-normal">set, then hold</th>
            <th className="text-left font-normal">contract requires</th><th className="text-left font-normal">simulated</th><th /></tr>
        </thead>
        <tbody>
          {steps.map((s) => (
            <tr key={s.id} className="border-t border-line align-top">
              <td className="py-1 pr-2"><span className="font-mono text-faint">{s.id}</span> {s.title}</td>
              <td className="pr-2 font-mono text-muted">{Object.entries(s.set).map(([k, v]) => `${k}=${v}`).join(" ")}{s.press ? `, ${s.press}` : ""}</td>
              <td className="pr-2"><Leds hex={s.expected} /> <span className="font-mono text-faint">{s.expected}</span>
                <div className="text-faint">{s.expected_text}</div></td>
              <td className="pr-2"><Leds hex={s.simulated} /> <span className={cx("font-mono", s.matches_contract ? "text-faint" : "text-bad")}>{s.simulated}</span></td>
              <td><Pill tone={s.matches_contract ? "ok" : "bad"}>{s.matches_contract ? "matches" : "differs"}</Pill></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-2xs text-faint">{note} LED15 = granted (green), LED14 = denied (red), LED7–LED0 = data.</p>
    </div>
  );
}
