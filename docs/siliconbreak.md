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
manager re-verifies the approval and the file hash before writing SRAM (never flash). The UI never records what the LEDs
physically showed; that is your observation on the board.

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
GET  /api/security/audits/{id}/fpga
POST /api/security/audits/{id}/fpga/{vulnerable|attempt-N}/build
POST /api/security/audits/{id}/fpga/{vulnerable|attempt-N}/program
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
   secret on LED7–LED0, the fixed board lights LED14 only.
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
| Basys 3 before/after: programming | board not connected | plug in the Basys 3, **Program…**, approve, then run demo steps D1–D4 on the board and compare with the listed LED patterns |
| Historical cases from public repositories | no case manifest with trusted tests written yet | a pinned historical case needs a contract, a trusted testbench and its fix commit; repository audits today stop at *suspected* |
