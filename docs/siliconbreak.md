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
| Factory Droid repair | `droid` CLI not installed, no `FACTORY_API_KEY` | install the CLI, add key, restart, **Repair with Factory Droid**; the attempt shows the droid session id, trace and refused actions |
| Baseline vs assisted comparison | needs a model key | **Run SiliconBench** after adding a key |
| Basys 3 before/after | `vivado_container` not running, board not connected | start your container, build the case's `board/basys3_top.v` wrapper for the vulnerable and fixed RTL, program with approval |
| Repository import / historical cases | not implemented yet | P1 |
