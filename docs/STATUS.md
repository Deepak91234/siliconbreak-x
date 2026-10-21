# CircuitLoop status (2026-10-21)

## Verified on this Mac

| Area | Evidence |
|---|---|
| Test suite | 320 pytest passed, 1 skipped (the opt-in Vivado test, which passes when run with `--run-vivado`); 6 vitest passed; `npm run build` clean |
| Demo 1, real end to end via the API | offline spec → template Verilog → lint (0 messages) → 23/23 simulation checks → Vivado synth/impl in `vivado_container` → timing met (WNS +5.239 ns) → valid `.bit` → board detection honestly `unavailable` |
| Demo 2, conversational follow-up | revision r2 is a `modification` of r1 (count down, reset on BTNC, fast mode kept) → 35/35 checks → Vivado rebuild (WNS +4.454 ns, 36 LUT, 78 FF) |
| Fault injection | `double_rate` planted in the core → 11 failing checks → repair `unavailable` (no API key) → Vivado skipped |
| Oracle strength | every example design passes; all 52 applicable planted defects are caught in simulation |
| Vision (synthetic frames only) | 36 tests: 3:1 ratio pass, 2:1 defect fail, wrong direction fail, dark/low-contrast/camera-moved → inconclusive |
| Programmer (no board attached) | openFPGALoader reports `no_board` with the real error text; programming refused without an approval matching the bitstream hash |
| Dashboard | live environment bar, chat, 14-stage pipeline, Verilog, Simulation, Vivado, Hardware tabs checked in the browser against real runs |

## Not yet tested (needs you)

1. **LLM path.** Put `ANTHROPIC_API_KEY=...` in `.env`, then run `.venv/bin/python scripts/llm_smoke.py` (makes real, billed calls). Then try Demo 1 in the dashboard. The revision should show `interpreter: llm:claude-opus-5-5`. Then try fault injection: repair should run instead of showing `unavailable`.
2. **Board detection and programming.** Your `start_container.sh` loop is running, so `xvcd` will claim the board's JTAG within about 2 s of plugging it in. CircuitLoop will then use the **hw_server** adapter, which is implemented but has never run against a real board:
   * Plug in the board, open the Hardware tab, and click **Detect**. It takes about 60 s because it starts a Vivado batch session. The state should become `connected` with idcode `0x0362d093`.
   * Click **Program FPGA**, then approve in the chat. The LEDs should start counting.
   * To test the native openFPGALoader path instead, stop the launcher (Ctrl+C in its terminal; that also stops the GUI container), plug in the board, and Detect again.
3. **Webcam verification.** In the Vision tab, allow the camera, draw a box around LED3 to LED0, set count = 4 and first LED = 0, then follow the guided phases (BTNU released, then held).

## Known limitations

* Mutation-based fault injection is written against the template generator's signal names, so on LLM-written cores most mutations report "does not apply" (`stuck_output` and `invert_output` work on any core).
* The Vision tab cannot find a session after a backend restart (sessions are in memory).
* The Claude app's Browser-pane server launcher cannot read `~/Desktop` without a macOS privacy permission. Start the servers with `./scripts/dev.sh` in a terminal instead.
* The Verilator harness build prints harmless clang warnings about unknown `-Wno-…` flags (Homebrew bottle flags vs Apple clang 15).
