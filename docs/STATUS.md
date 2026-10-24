# CircuitLoop status (2026-10-23)

## Verified on this Mac

| Area | Evidence |
|---|---|
| Test suite | 330 pytest passed, 1 skipped (the opt-in Vivado test, which passes when run with `--run-vivado`); 11 vitest passed; `npm run build` clean |
| Demo 1, real end to end via the API | offline spec → template Verilog → lint (0 messages) → 23/23 simulation checks → Vivado synth/impl in `vivado_container` → timing met (WNS +5.239 ns) → valid `.bit` → board detection honestly `unavailable` |
| Demo 2, conversational follow-up | revision r2 is a `modification` of r1 (count down, reset on BTNC, fast mode kept) → 35/35 checks → Vivado rebuild (WNS +4.454 ns, 36 LUT, 78 FF) |
| Fault injection | `double_rate` planted in the core → 11 failing checks → repair `unavailable` (no API key) → Vivado skipped |
| Oracle strength | every example design passes; all 52 applicable planted defects are caught in simulation |
| Vision (synthetic frames only) | 36 tests: 3:1 ratio pass, 2:1 defect fail, wrong direction fail, dark/low-contrast/camera-moved → inconclusive |
| Programmer (no board attached) | openFPGALoader reports `no_board` with the real error text; programming refused without an approval matching the bitstream hash |
| hw_server adapter, up to the board | its Tcl session runs in the real `vivado_container` (Vivado 2023.2), connects to your GUI's `hw_server`, and with no board cleanly reports `no_board` (Vivado's real "Labtoolstcl 44-494 no active target" message). Only the program step itself is left untested |
| Webcam pipeline through the API | synthetic camera frames streamed over `/ws/vision` in the browser's binary format, `auto-roi`, guided phases, finish → `passed` with ratio ≈ 3, verdict stored on the revision, evidence plot served |
| RTL safety checker | blocks `$system`, every `$c` variant, file I/O, `` `include ``/`` `define ``, DPI, `bind`, extra modules, derived clocks before any Verilator run |
| Dashboard | live environment bar, chat, 14-stage pipeline, Verilog, Simulation, Vivado, Hardware tabs checked in the browser against real runs |

## SiliconBreak X (security extension, branch feature/siliconbreak-x)

Verified with real Verilator 5.052 and Yosys 0.69: three security cases with valid harnesses (vulnerable fails its security tests, reference passes everything, formal counterexample then proof); the full audit pipeline through the API and the Security tab (offline rules, reproduction, formal, evidence, harness validation); SiliconBench from the UI (harness 3/3, reproduction 3/3, formal 3/3, offline-rules detection 1/3; model and Droid arms "not run" without keys). Bitdeer, Claude and Factory Droid paths are implemented and unit-tested with fakes but have not run against the real services. P1: public-repository import at a pinned commit (verified live against OpenTitan `rv_dm`), repository and design audits that stay at *suspected*, and Basys 3 before/after builds with a board-level simulation of the demo sequence (verified); both demo bitstreams (vulnerable and reference fix) are built in an isolated `siliconbreak_vivado` container that mounts only a dedicated folder and the Vivado install read-only (timing met, WNS +5.5/+5.1 ns); programming is not done (no board, and it needs your approval). Details and remaining work: [siliconbreak.md](siliconbreak.md).

## Dashboard (redesign, browser-validated)

Validated in a browser against a real backend at 1440 px, 1024 px and 375 px: the welcome workspace with example cards that fill the real prompt; a live traffic-light run streaming over the WebSocket through every stage to a validated bitstream, with detection honestly `unavailable`; the 2D twin highlighting a guide step's controls; a scenario replay animating LEDs from the Verilator trace; the virtual FPGA (Demo 2 wraps 0→15 after 1 s, BTNC holds 0000, and it resumes after release); the 3D view rendering; the block diagram; the revision comparison; Verilog search; and presentation mode with a labelled replay. Panels resize, and below 1280 px and 900 px they become drawers with no horizontal overflow.

Modes and provenance: **Preview** uses the bundled example specs with highlights and steps only (no LED states). **Simulation** shows only Verilator data (replay or virtual FPGA). **Live hardware** shows backend-confirmed programming and webcam-observed LEDs only. Every LED state carries its source label.

## What remains to connect or test for full end-to-end operation

| Integration | How to test | What should change in the UI |
|---|---|---|
| Anthropic model | Add `ANTHROPIC_API_KEY` to `.env`, open the AI-mode chip, then **Reload configuration**. Run `scripts/llm_smoke.py` (billed), then a design and a fault-injection run | The chip says "Model agent"; revisions show `interpreter: llm:…`; the repair stage runs instead of showing `unavailable` |
| Board detection (hw_server over XVC) | Plug in the Basys 3 (your launcher loop starts `xvcd`), then press **Detect** in the Connection tab or the hardware chip. Allow 60–90 s for Vivado startup | Hardware state becomes `connected` with idcode `0x0362d093` |
| Programming | Click **Program FPGA…** in Live mode or the Hardware tab, then approve in the chat | The timeline Program stage passes, the inspector shows "rN programmed", and Guided hardware mode applies to the real board |
| Webcam verification | In the Camera tab, allow the camera, draw a box around the LEDs, and run the guided phases | Live mode shows **Observed (webcam)** LED states on the twin; the verify stage records pass/fail/inconclusive with evidence |
| Live-mode observed LEDs on the twin | During a running camera session, switch the inspector to Live | LED states come from `vision.updated` live states. This was tested only with synthetic frames through the API, not yet from a real camera into the twin |

## Not yet tested (needs you)

1. **LLM path.** Put `ANTHROPIC_API_KEY=...` in `.env`, then run `.venv/bin/python scripts/llm_smoke.py` (makes real, billed calls). Then try Demo 1 in the dashboard. The revision should show `interpreter: llm:claude-opus-5-5`. Then try fault injection: repair should run instead of showing `unavailable`.
2. **Board detection and programming.** Your `start_container.sh` loop is running, so `xvcd` will claim the board's JTAG within about 2 s of plugging it in. CircuitLoop will then use the **hw_server** adapter, which is implemented but has never run against a real board:
   * Plug in the board, open the Hardware tab, and click **Detect**. It takes 60–90 s because it starts a Vivado batch session. The state should become `connected` with idcode `0x0362d093`. (With no board it already reports `no_board` correctly.)
   * Click **Program FPGA**, then approve in the chat. The LEDs should start counting.
   * To test the native openFPGALoader path instead, stop the launcher (Ctrl+C in its terminal; that also stops the GUI container), plug in the board, and Detect again.
3. **Webcam verification.** In the Vision tab, allow the camera, draw a box around LED3 to LED0, set count = 4 and first LED = 0, then follow the guided phases (BTNU released, then held).

## Known limitations

* A camera verdict is attributed to a revision (and can upgrade its run to `passed`) only if CircuitLoop programmed that revision; otherwise it is stored with `programmed_by_circuitloop: false`.
* Mutation-based fault injection is written against the template generator's signal names, so on LLM-written cores most mutations report "does not apply" (`stuck_output` and `invert_output` work on any core).
* The Camera tab cannot resume a live session after a backend restart (sessions are in memory; finished results are stored).
* The board drawing follows the real component order, labels and arrangement, but positions approximate the board drawing; they are not CAD data.
* `npm audit` reports 12 advisories in frontend dependencies (mostly dev tooling); not addressed.
* A backend restart is needed for a running `./scripts/dev.sh` to serve the new endpoints (the Vite server hot-reloads the UI, but the old backend returns 404 for the guide and board endpoints).
* The Claude app's Browser-pane server launcher cannot read `~/Desktop` without a macOS privacy permission. Start the servers with `./scripts/dev.sh` in a terminal instead.
