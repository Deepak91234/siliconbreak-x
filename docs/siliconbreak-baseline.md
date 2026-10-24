# SiliconBreak X baseline (M0, 2026-10-24)

Snapshot of CircuitLoop before the security extension, taken on branch
`feature/siliconbreak-x` (cut from `main` at `e092601`, working tree clean).

## What already worked and is reused

| Capability | Source | Reused by SiliconBreak X |
|---|---|---|
| Runs, stages, events, WebSocket fan-out, replay | `backend/core/events.py`, `backend/storage/`, `/ws/events` | Audits are runs (`trigger = security_audit`) with their own stage list; `security.updated` events |
| Restricted subprocess runner (argv only, timeouts, cancellation) | `backend/core/process.py` | Every Verilator/Yosys call |
| Verilator lint policy and message parser | `backend/verification/verilator.py` | The trusted verifier's lint step |
| Comment-stripping Verilog lexer | `backend/rtl/safety.py` | The host-safety scan and the offline rules |
| VCD to JSON | `backend/verification/waveform.py` | Security waveforms |
| Anthropic provider (structured output) | `backend/agent/llm/` | Claude investigator; Bitdeer adapter implements the same protocol |
| Vivado-in-Docker batch builds, board pins, approval-gated programming | `backend/vivado/`, `boards/basys3/`, `backend/programmer/` | Untouched; the demo case ships a trusted Basys 3 wrapper for a later build |
| Three-panel dashboard, timeline, 2D/3D twin, diff view | `frontend/src/` | Security and SiliconBench tabs added; 3D falls back to 2D without WebGL |

## Baseline checks (exact commands and results)

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider     # 330 passed, 1 skipped (346 s)
cd frontend && npx tsc -b && npx vitest run           # type check clean, 11 passed
```

One earlier full run stopped on a transient `test_virtual_fpga_runs_real_rtl` timeout while the
machine was heavily loaded; the test passes alone (9 s) and in the following full run.

## Environment observed

| Item | State |
|---|---|
| Docker | 29.8.1 |
| `vivado_container` (image `x64-linux`, linux/amd64 under Rosetta) | Running when the session started (up 3 days); **no longer present** later the same day (`docker ps -a` does not list it). CircuitLoop correctly reports "no Vivado container is running" and offers, but never starts, a headless container. |
| Verilator | 5.052 (Homebrew) |
| Yosys | 0.69+post (Homebrew); `yosys-smtbmc` present; no SymbiYosys, no external SMT solver. Formal checks use Yosys's built-in SAT solver instead. |
| openFPGALoader | v1.1.1 |
| Basys 3 | not connected |
| Model keys | none in `.env` (`ANTHROPIC_API_KEY`, `BITDEER_API_KEY`, `FACTORY_API_KEY` all unset) |
| Factory Droid | `droid-sdk` 0.5.0 installed in `.venv`; the `droid` CLI is **not installed** (system-level install, left to the user) |

## Known issues carried over

* The screenshot that motivated the plan showed the 3D inspector failing with "Error creating
  WebGL context"; the inspector now shows the 2D twin with a notice in that case.
* `npm audit` advisories in frontend dev tooling (unchanged).
