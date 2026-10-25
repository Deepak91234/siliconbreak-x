# SiliconBreak X

Hardware-security investigation, repair and verification, built as an additive
extension of CircuitLoop. One rule governs everything: **an AI description of a
bug is a suspicion; only the trusted verifier's result is evidence.**

## Pipeline

```text
Security tab / POST /api/security/audits
        │
        ▼  (a CircuitLoop run with these stages; events over /ws/events)
contract ─ index ─ investigate ─ reproduce ─ formal ─ repair ─ retest ─ evidence
              │          │            │         │        │        │
              │   Bitdeer / Claude    │   Yosys SAT   isolated   same verifier
              │   or offline rules    │   (proof or   copy:      + formal on
              │   → "suspected"       │   counter-    Droid /    the patch
              │                       │   example)    model /
              │              trusted verifier         reference
              │              (Verilator, nonce)
        security_cases/<id>/case.json  (contract, tests, formal wrapper, hidden reference)
```

| Module | Role |
|---|---|
| `security_cases/<id>/` | Case bundle: editable RTL, trusted testbench (`tests/*.sv`), formal property wrapper, hidden reference fix and ground truth, optional trusted Basys 3 wrapper |
| `backend/security/cases.py` | Manifest loader; `public_view()` is all an agent or the UI may see |
| `backend/security/verify.py` | **Trusted verifier**: copies candidates, enforces the editable allowlist, host-safety scan, records hashes, Verilator lint, builds the trusted testbench with `--binary --timing`, runs it with a per-run nonce, parses only nonce-tagged verdicts |
| `backend/security/formal.py` | Yosys `sat -tempinduct` over the trusted wrapper: `proven` / `counterexample` / `bounded` (not a proof) |
| `backend/security/hostsafety.py` | Rejects `$system`, `$c`, file/plusarg I/O, all print/finish tasks, DPI, `bind`, `` `include `` before any compile |
| `backend/security/investigate.py`, `rules.py` | Provider routing (Bitdeer → Claude → offline rules), prompt with sources as delimited untrusted data, validation of every cited file/line/property |
| `backend/agent/llm/bitdeer_provider.py` | Bitdeer Model Studio (OpenAI-compatible) adapter: json_schema → json_object fallback, think-tag stripping, 401/402/429 classification, key scrubbing |
| `backend/security/repair.py` | Isolated workspaces (editable RTL + `TASK.md` only), Factory Droid session with an allowlist permission handler, model-patch repair, human reference fix, scope detection and diffs |
| `backend/security/orchestrator.py` | The audit run and the finding status ladder |
| `backend/security/bench.py` | SiliconBench arms and metrics |
| `frontend/src/security/` | Security tab, SiliconBench view, provider status, waveform |

## Trust boundary

* The verifier owns pass/fail. Its testbenches and formal wrappers live only in the case
  directory; their sha256 is recorded in every report. Agents never receive them.
* Agents work in a fresh copy containing only the editable RTL and `TASK.md` (contract plus the
  verifier's failing test ids, messages and counterexample inputs). Any change outside the
  editable files is a scope violation and the patch is rejected.
* Factory Droid runs with `Autonomy.OFF`; every permission request passes through
  `make_permission_handler`, which allows only edits/creates/patch-updates of allowlisted files
  inside the copy and refuses every shell command.
* Candidate RTL is scanned before Verilator compiles it (Verilator would otherwise execute
  `$system` or inline C++). Print tasks are banned so a design cannot print fake verdict lines,
  and the verifier additionally requires a nonce the design cannot read.
* Model output is structured JSON validated locally; findings citing unknown files, lines outside
  the file, or no contract property are dropped and listed.
* Keys (`BITDEER_API_KEY`, `ANTHROPIC_API_KEY`, `FACTORY_API_KEY`) stay in the backend process;
  `/api/security/health` reports states, never keys.

## Repository and design audits (P1)

* **Import:** `POST /api/security/repos {url, ref?, subdir?}`. Only `https://github.com/<owner>/<repo>` URLs are accepted, and the ref is
  resolved to a 40-hex SHA with `git ls-remote` first. The import is a shallow partial fetch (`--depth 1 --filter=blob:none`) with a
  sparse checkout of `*.v *.sv *.vh *.svh` (optionally under one subdirectory). Hooks are disabled, symlinks are checked out as
  plain files and skipped, submodules and LFS are never fetched, prompts are off (private repositories simply fail), and file
  count and byte caps apply. Nothing from the repository is executed. Example: OpenTitan `hw/ip/rv_dm/rtl` at
  `478afb033bb2d3e62cb4c5dd097f578de461b2b5` imports in about 2 s (8 files, 13.5k lines).
* **Index:** modules, packages, includes, approximate instantiations and a keyword-based security-relevance score, which only
  orders files for review.
* **Audit:** `POST /api/security/audits {repo_id, files?}` (explicit files, or the highest-scoring files within the context budget,
  at most 12), or `{project_id, revision_id?}` for the current CircuitLoop design's generated RTL. These audits use a **generic**
  hardware-security contract (G-ACCESS, G-DEBUG, G-LOCK, G-ZEROIZE, G-RESET, G-FSM). With no trusted tests, reproduction,
  formal checks and repairs are recorded as *not run*, every finding stays **suspected**, and the outcome is `suspected_only`
  or `no_findings` ("not a proof of security"). Findings link to the exact lines at the pinned commit. The UI and the report
  show which files were indexed and which were actually read.

## Basys 3 before / after (P1)

The demo case ships a trusted Basys 3 wrapper (`board/basys3_top.v`: BTNU read request, SW1 authorized, SW2 debug, SW3 address,
BTNC reset; LED7–LED0 data, LED15 granted, LED14 denied) and a demo sequence whose LED patterns come from the contract.
`POST /api/security/audits/{id}/fpga/{variant}/build` (variant `vulnerable`, or `attempt-N` for a verifier-accepted patch whose bytes
still match the verified hashes) runs:

1. **Board-level simulation.** The full Basys 3 top (wrapper, CircuitLoop library and the variant's RTL) runs the demo sequence
   in CircuitLoop's generic Verilator harness, with debounce shortened by parameter. Each step's simulated LEDs are compared
   with what the contract requires. Vulnerable: D1 (unauthorized debug read) shows `0x80A5`, i.e. granted plus the secret, where
   `0x4000` (denied) is required. Reference fix: all four steps match. This is simulation, never a hardware observation.
2. **Vivado.** Synthesis, implementation, timing and bitstream through the existing batch flow in your container, with pins from
   the trusted board definition (`board_xdc`). Without a running container, the existing approval-gated "start headless
   container" request is raised; nothing starts without your click, and if declined the stages report *unavailable*.

`POST .../fpga/{variant}/program` detects the board, then raises the existing SHA-bound `program_fpga` approval; the hardware
manager re-verifies the approval and the file hash before writing SRAM (never flash). What the LEDs physically showed is
only recorded through webcam evidence (next section); otherwise it is your own observation.

### Webcam evidence of the demo steps

**Verify with webcam** on a variant records what the board's LEDs actually showed at each demo step, as physical evidence
next to the contract and the board-level simulation (`backend/security/board_vision.py`, UI `frontend/src/security/BoardCamera.tsx`).

- **Attribution.** A camera cannot tell which bitstream is loaded, so a session can only be opened for the variant whose
  bitstream CircuitLoop programmed last: the most recent executed `program_fpga` approval (security or regular flow) must name
  this audit, this variant and this build's SHA-256. Otherwise the request is refused (409) with the reason. A power cycle since
  programming is not detectable here; it shows up as a mismatch with the simulation (verdict *unexpected*).
- **Steps.** A dark reference (no button held, every LED off; the controller clears its outputs when `read_req` drops), then
  D1-D4 from the case. For each step you set the switches, press **Capture** and hold BTNU: 2 s to get a hand on the button,
  then at least 3 s of recording, extended (up to 9 s more) until 24 frames have reached the backend, because a slow machine or
  a throttled background tab streams fewer frames.
- **Measurement.** The existing webcam pipeline: frames stream over `/ws/vision/{id}`; one box around LED15..LED0 is split into
  16 cells and snapped to the LEDs; per-LED green level in each frame; camera-motion and darkness rejection. At **Finish** every
  step is re-read with on/off levels calibrated over the whole session (LEDs that never light borrow the levels of LEDs that
  did), ignoring the first 400 ms of each step. An LED counts only with at least 6 classified frames and 75% agreement; a step
  with any unread LED, or shorter than 1 s, is *inconclusive*, never guessed.
- **Verdict.** *violation_observed* (every step read, all match this variant's simulation, at least one breaks the contract),
  *contract_met* (all match the contract), *unexpected* (the board differs from the simulation of the bitstream it should be
  running) or *inconclusive* (a step missing or unreadable).
- **Evidence.** `fpga/<variant>/camera/<session>/observation.json`, `frames.csv` (per-frame levels of all 16 LEDs) and
  `snapshots/<step>_<n>.jpg` (a frame from inside the step window with the cells drawn green/grey for the reading). The
  result is appended to `record.fpga.<variant>.observations` and shown under the variant in the Security tab.

Tested with the synthetic board renderer (vulnerable leak observed, patched contract met, board/simulation mismatch, missing
step, dark scene, short capture) and through the real HTTP + WebSocket API; checked in the dashboard with a canvas standing in
for the webcam. Not yet run with the real webcam on the real board.

## AI credit protection

Sponsor credits: Claude $50, Bitdeer $50, Factory Pro (one month; a $20 voucher is not assumed). Every paid call goes
through `backend/core/budget.py`, including CircuitLoop's own design agent:

* **Live AI is off until you turn it on** (Security tab or SiliconBench → *AI credits* → *Turn on live AI*, which
  shows the caps and asks for confirmation). While off, keys may be present but no request is sent: investigations
  use the offline rules (labelled not AI) and only the human reference fix can run.
* **Worst-case reservation before each call:** input estimated at 3 characters per token (high) plus `max_tokens` at
  the output price (thinking bills as output). Refused above **$1.00 per call**, **$15 per provider per day**, or the
  provider total (**$40 Claude, $40 Bitdeer**, leaving $10 of each grant in reserve). In-flight reservations count.
* **Settled with reported usage**; when a request may have been billed without usage (timeout, truncated or
  refused output) the worst case stays on the books.
* **Prices:** Claude Opus 5.5 $4 / $20 per MTok, Sonnet 5.5 $2 / $10, Haiku 5.5 $0.10 / $0.50; unknown models are
  costed at $10 / $50. Bitdeer publishes no machine-readable per-model prices, so it is costed at a deliberately
  high $1 / $4 (`SILICONBREAK_BITDEER_PRICE_IN/OUT` to change). Worst case per audit call: about $0.34 (Opus 5.5),
  $0.07 (Bitdeer at the placeholder price).
* **Factory Droid** reports credits, not dollars, and has no spend cap of its own: at most **6 sessions per day** and
  **2 per repair**, sessions time out after 10 minutes, and reported credits are recorded. Keep *Extra Usage* off in
  Factory's settings so nothing bills beyond the plan.
* **Nothing spends automatically:** an audit makes one investigation call; repairs run only from the *Repair*
  buttons (each shows its worst case and asks first); SiliconBench runs the free arms by default, and paid arms need
  a confirmed worst-case total (the API refuses with 409 otherwise). A full paid run over the three cases is at most
  about $2.02 plus 6 Droid sessions.
* **Tests never spend:** an autouse fixture hides the keys, keeps live AI off and gives each test a throwaway data
  folder; billed tests would need the `live_ai` marker and `--run-live-ai`.
* Ledger: table `ai_usage` in the CircuitLoop database; `GET /api/ai/budget`, `POST /api/ai/live {enabled}`,
  `POST /api/bench/estimate`.

Settings (`.env`): `SILICONBREAK_LIVE_AI`, `SILICONBREAK_BUDGET_ANTHROPIC_USD`, `SILICONBREAK_BUDGET_BITDEER_USD`,
`SILICONBREAK_BUDGET_DAILY_USD`, `SILICONBREAK_BUDGET_PER_CALL_USD`, `SILICONBREAK_DROID_SESSIONS_PER_DAY`,
`SILICONBREAK_DROID_MAX_ATTEMPTS`, `BITDEER_MODEL` (this account offers `zai-org/GLM-5.3`, `GLM-5.3-Flash`,
`DeepSeek-V4.1-Flash`, `DeepSeek-V4-Flash`, `Qwen3.8-27B`; configured: `zai-org/GLM-5.3`).

### First live runs (2026-10-24)

* **Bitdeer investigation (GLM-5.3):** the first call ran away (analysis inside `summary`, no `findings`, then a
  repeated sentence until max_tokens) and the pipeline fell back to the offline rules. After requiring `findings`,
  bounding text fields, adding a mild repetition penalty and an 8k token cap, a live call returned the correct finding
  (`rtl/debug_access_ctrl.v` 47-54, P-NOLEAK + P-DEBUG, CWE-1191, high confidence) in 12 s, about $0.005. Ledger
  total: $0.079 over 3 calls (mostly the worst case kept for the first, truncated call).
* **Factory Droid repair:** Droid found the right fix every time, but its edits were cancelled: droid-sdk 0.5.0 cannot
  parse the CLI 0.249 permission request for an ApplyPatch made through the `Script` tool (`confirmationType:
  "script"`, details `calls[].confirmation`), and passing any tool restriction hid the edit tool. A wire-level handler
  now approves a script request only when every inner operation is an update of an allowlisted file. Live result
  (session `fcf47602-...`, model `gpt-6.1-sol`): one approved edit, `if (authorized || debug_mode)` -> `if (authorized)`,
  **12/12 trusted tests passed, formal proven**. Six Droid sessions used in total (about 94.5k Factory credits
  reported); the daily cap stopped further sessions.

### First full end-to-end run on the Basys 3 (2026-10-24, audit `sa_b912ae783f`)

1. **Investigation (Bitdeer GLM-5.3):** suspected the `authorized || debug_mode` bypass at lines 49-51 (P-NOLEAK, P-DEBUG).
2. **Independent reproduction:** 4 trusted security tests failed (functional 7/7 passed); formal counterexample.
3. **Repair (Factory Droid, session `da09e5b9-...`, 1 attempt):** one approved edit, `if (authorized)`; retest
   12/12, formal proven. Finding history: suspected -> reproduced -> patch_proposed -> repaired_verified.
4. **Bitstreams (isolated `siliconbreak_vivado`):** before WNS +5.528 ns (sha256 `33b23b0c...`), after WNS
   +5.119 ns (sha256 `15a6eba0...`); board-level simulation: before leaks at D1, after matches all 4 steps.
5. **Programming (openFPGALoader, SRAM only, each approved in the dashboard):** both bitstreams loaded 100%.
6. **On the board (observed by the user, not camera-verified):** before: SW1 off, SW2 on, SW3 on + BTNU lit LED15
   and LED7/5/2/0 (`1010 0101`, the secret). After: the same inputs lit only LED14 (denied); with SW1 on, LED15 and
   `1010 0101` (the authorized read still works).

Spend for the day: Bitdeer $0.091 booked over 4 calls (mostly the worst case kept for the first truncated call),
Claude $0, Factory Droid 7 sessions (about 1% of the plan's usage limits; Extra Usage balance $0, overflow set to
Droid Core).

## Isolated Vivado (protecting the university setup)

The `x64-linux` image contains only Ubuntu and libraries; Vivado 2023.2 (34 GB) lives inside the coursework bind
mount at `~/cse100/cse100-vivado-on-silicon-mac-main/Xilinx`. SiliconBreak X builds therefore run in a separate
container that never sees the coursework:

| Host | `siliconbreak_vivado` | Mode |
|---|---|---|
| `~/siliconbreak_vivado` | `/home/user` | read-write: build workspace `ws/`, logs, Vivado's own fresh `~/.Xilinx` |
| `~/cse100/…/Xilinx` | `/home/user/Xilinx` | **read-only**: the Vivado install only |

It runs with `--network none`, no ports, `--memory 2560m --memory-swap 2560m --oom-score-adj 1000 --cpus 3`, `--rm`.
`vivado_container`, the coursework folders, `.Xilinx` settings and the launcher are never mounted, stopped or changed.
CircuitLoop is pointed at it through `.env` (`CIRCUITLOOP_VIVADO_CONTAINER`, `CIRCUITLOOP_VIVADO_HOST_ROOT`,
`CIRCUITLOOP_VIVADO_INSTALL_DIR`, `CIRCUITLOOP_VIVADO_WORKSPACE_SUBDIR=ws`, `CIRCUITLOOP_VIVADO_MAX_THREADS=2`). With
`CIRCUITLOOP_VIVADO_INSTALL_DIR` set, CircuitLoop refuses any container whose install mount is not read-only or whose
home mount is not the dedicated folder, and its own "start headless container" button creates exactly this container.
Verified inside the container: the install is mounted `ro` (writes fail), only `ws`/`Xilinx` are visible, no network.

**Built on 2026-10-24** (audit `sa_f158563bbb`, data in `runs/siliconbreak_isolated/`):

| Variant | Board-level sim | Timing (100 MHz) | LUT / FF | Bitstream sha256 |
|---|---|---|---|---|
| before: vulnerable | D1 differs (`0x80A5` vs `0x4000`) | WNS +5.528 ns, WHS +0.122 ns | 54 / 128 | `e6701e493f96797c7ef8ed90cfba4a6ae398fde2bcb52a23f7ddcf8291d2e55e` |
| after: human reference fix | all 4 steps match | WNS +5.119 ns, WHS +0.131 ns | 53 / 128 | `47e01531540653dfa21294d4654cc57f9fd7469de677e9dd891e82a83790bf43` |

Both 2,192,118 bytes, Vivado 2023.2 in `siliconbreak_vivado` (224 s and 333 s). Nothing under `~/cse100` changed during the
builds; the only file activity there came from an older CircuitLoop backend still running with the previous
configuration (transient `.probe` tokens in `circuitloop_ws/`), which a restart of `./scripts/dev.sh` ends. The board has
not been programmed.

## Status ladders

Findings: `suspected → reproduced → patch_proposed → repaired_verified` (or `repair_failed`,
`false_positive` only when the tests pass **and** formal proves the cited properties,
`inconclusive` otherwise). A human reference fix is recorded as **harness validation** and never
moves a finding past `reproduced`.

Audit outcomes: `reproduced`, `repaired_verified`, `repair_failed`, `no_violation_found`, `error`.

## Cases

| Case | Kind | CWE | Ground truth |
|---|---|---|---|
| `debug_bypass_demo` | demo (intentionally vulnerable; excluded from historical metrics) | CWE-1191, CWE-284 | SECRET branch grants on `authorized \|\| debug_mode` |
| `lock_bit_clear` | locally authored, injected defect | CWE-1231 | CONTROL write assigns `lock_q <= wr_data[0]` |
| `zeroize_incomplete` | locally authored, injected defect | CWE-1239, CWE-226 | zeroize clears only `key_q[7:0]` |

Every case is validated by `tests/test_security.py::test_every_case_harness_is_valid`: the
vulnerable source fails a security test while its functional tests pass, the reference fix
passes every test, and formal finds a counterexample on the vulnerable source and proves the
reference. Public benchmark tasks (HardSecBench, HWE-Bench) are not imported yet: their licenses
and runner compatibility have not been checked.

## API

```text
GET  /api/security/health[?refresh=true]
GET  /api/security/cases
GET  /api/security/cases/{case_id}/source
POST /api/security/audits                {case_id, investigator?, repair_agent?: auto|factory|model|reference|none}
GET  /api/security/audits[?project_id=]
GET  /api/security/audits/{id}
POST /api/security/audits/{id}/repair    {agent: auto|factory|model|reference}
GET  /api/security/audits/{id}/file?path=        text artifacts (report.md, audit.json, logs, TASK.md, droid trace)
GET  /api/security/audits/{id}/sources[?attempt=N]
GET  /api/security/audits/{id}/waveform?path=&start=&end=&signals=
POST /api/security/audits                {repo_id, files?} | {project_id, revision_id?}
POST /api/security/repos                 {url, ref?, subdir?}
GET  /api/security/repos, /api/security/repos/{id}, /api/security/repos/{id}/file?path=
GET  /api/security/audits/{id}/fpga                 variants, demo steps, state, provenance (what CircuitLoop last programmed)
POST /api/security/audits/{id}/fpga/{vulnerable|attempt-N}/build
POST /api/security/audits/{id}/fpga/{vulnerable|attempt-N}/program
POST /api/security/audits/{id}/fpga/{vulnerable|attempt-N}/camera   webcam session (409 unless that variant is loaded)
GET  /api/security/camera/{sid}
POST /api/security/camera/{sid}/auto-roi             {box: [x, y, w, h]} normalised, around LED15..LED0
POST /api/security/camera/{sid}/step                 {action: start, step_id} | {action: end}
POST /api/security/camera/{sid}/finish               -> observation (also stored in the audit record)
GET  /api/security/audits/{id}/camera-image?path=     snapshot JPEGs under fpga/*/camera/
WS   /ws/vision/{sid}                                 frames: [float64 LE t_ms][JPEG], as for design verification
GET  /api/bench/cases
POST /api/bench/runs                     {case_ids?, arms?, max_attempts?}
GET  /api/bench/runs, /api/bench/runs/{id}
```

Live progress: the existing `/ws/events` stream (`run.*`, `stage.updated`, `log`,
`agent.message`, plus `security.updated` and `bench.updated`).

## Running it

```bash
./scripts/dev.sh            # restart it if it was already running: the backend has new routes
# open http://127.0.0.1:5173, then Security tab (or "Audit" beside the project picker)
.venv/bin/python -m pytest -q tests/test_security.py
```

To enable the AI paths, add to `.env` and restart the backend:

```bash
BITDEER_API_KEY=...        # preferred investigator (model: BITDEER_MODEL, default deepseek-ai/DeepSeek-V4-Pro)
ANTHROPIC_API_KEY=...      # Claude investigator / model-patch repairs
FACTORY_API_KEY=...        # Factory Droid; also install the droid CLI so `droid` is on PATH
```

## Demo script (three clicks)

1. Security tab → **Run security audit** on *Debug-mode access-control bypass*. Watch the stages:
   the suspected finding at `rtl/debug_access_ctrl.v:49`, then 4 trusted security tests failing
   (functional 7/7 still passing), the waveform of `S-UNAUTH-DEBUG-READ` showing `read_grant`
   and `0xA5` with `authorized = 0`, and the 3-step formal counterexample.
2. **Repair with Factory Droid** (or **Repair with model**): the diff, the independent retest and
   formal on the patch. Without keys, use **Validate harness (reference fix)** and say so: it is
   the human reference, labelled as such, showing the verifier accepts a correct fix (12/12, proven).
3. SiliconBench → **Run SiliconBench**: harness 3/3, reproduction 3/3, formal 3/3, offline rules
   detect 1/3; model and Droid arms show "not run" until keys are configured.
4. Back in the audit, **Basys 3 before / after → Simulate + build** on *before* and *after*: the demo-step table shows D1 leaking
   `0xA5` on the vulnerable board top and denying on the fixed one (simulation). With Vivado running and the board connected,
   **Program…** each one (approval) and press BTNU with SW1 off, SW2 on, SW3 on: the vulnerable board lights LED15 and the
   secret on LED7–LED0, the fixed board lights LED14 only. After each programming, **Verify with webcam** records it:
   frame the LEDs, capture the dark reference and D1-D4, **Finish**: *violation observed on the board* before, *contract met
   on the board* after.
5. Optional: **Audit a public repository → Example: OpenTitan rv_dm → Import → Audit**: pinned SHA, the files actually read,
   and the honest "suspected only / not a proof" outcome.

## What is verified, and what is not

Verified on this Mac (real Verilator 5.052 and Yosys 0.69, no mocks in the pipeline):
the full audit with offline rules, reproduction, formal, harness validation, evidence; the
SiliconBench run above; the model-repair path with a scripted provider (accepted when correct,
rejected when it breaks functionality); the Droid permission policy against real `droid_sdk`
types; the Bitdeer adapter against a mock HTTP server; the UI in the browser.

Not yet run (needs you):

| Item | Blocker | How to test |
|---|---|---|
| Bitdeer investigation | no `BITDEER_API_KEY` | add key, restart, Security → Run audit; the investigator chip shows Bitdeer and the model id |
| Claude investigation / model repair | no `ANTHROPIC_API_KEY` | add key, restart, **Repair with model** |
| Factory Droid repair | `droid` CLI 0.249.0 is now installed (`~/.local/bin/droid`); no `FACTORY_API_KEY` | log the CLI in or set `FACTORY_API_KEY`, then **Repair with Factory Droid** (explicit click). Automatic repairs during an audit use Droid only when `FACTORY_API_KEY` is set. The attempt shows the session id, trace and refused actions |
| Baseline vs assisted comparison | needs a model key | **Run SiliconBench** after adding a key |
| Basys 3 before/after: Vivado build | done in the isolated `siliconbreak_vivado` (see above) | rebuild from the Security tab after an agent repair to get an agent-patched *after* |
| Basys 3 before/after: programming | done (see the end-to-end run above) | |
| Webcam evidence on the real board | not run yet | program a variant, **Verify with webcam**, capture the dark reference and D1-D4, **Finish** |
| Historical cases from public repositories | no case manifest with trusted tests written yet | a pinned historical case needs a contract, a trusted testbench and its fix commit; repository audits today stop at *suspected* |
