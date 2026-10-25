# UI feature inventory: simplification pass (2026-10-25)

Checkpoint before the pass: git tag `checkpoint/pre-simplify` (commit `a904b56`).
Goal: fewer visible panels, same functionality. Every capability below must stay operable in its new place
(a link or placeholder does not count). "Verified" comes from browser checks on 2026-10-25 against the real backend
(read-only) and, for the webcam flow, a keyless scratch copy of the data; actions that spend credits, start Vivado or
program the board were checked for presence and gating but not clicked.

## Global

| # | Capability | Before | After | Verified |
|---|---|---|---|---|
| G1 | Section navigation | sidebar: Dashboard, Security Lab, SiliconBench, FPGA Lab, Workspace | sidebar: Security Lab (default), SiliconBench, FPGA Lab, Workspace; old Dashboard links open Security Lab || yes: 4 areas; `#tab=dashboard` opens Security Lab |
| G2 | Environment status: backend/socket, Docker, Vivado, Verilator, openFPGALoader, board, AI, each with details | 7 pills in the top bar | one **System status** menu, a row per item with the same details || yes: 7 rows with details |
| G3 | Start Vivado container (approval) / stop headless container | Vivado pill popover | System status → Vivado row || yes: Vivado row keeps the start/stop actions (shown when applicable) |
| G4 | Refresh environment | top-bar button | System status menu || yes |
| G5 | Pending approvals: approve / deny | Approvals tray | Approvals tray (Pending) || yes |
| G6 | Approval history | only inside chat messages | Approvals tray (History tab) + chat || yes: History tab (4 decided approvals), each expands to its card |
| G7 | Presentation mode | top-bar button | top-bar button || yes: opens and exits |
| G8 | Theme toggle | top-bar button | top-bar button || yes: both themes render |
| G9 | AI provider configuration dialog | AI pill → dialog | **Settings & usage** → AI provider || yes: Settings & usage → Configure opens the dialog |
| G10 | AI credits: ledger, caps, live-AI switch | sidebar meters + panels in Security Lab, SiliconBench, Dashboard | **Settings & usage** (one place) || yes: ledger, caps, live-AI switch |
| G11 | Provider and tool health | panels in Security Lab | **Settings & usage** || yes |
| G12 | Recent audits / designs shortcuts | sidebar | Security Lab audit list / Workspace project selector || yes |
| G13 | Sidebar collapse, connection banner, toasts | shell | shell || yes |

## Security Lab

| # | Capability | Before | After | Verified |
|---|---|---|---|---|
| S1 | Start an audit of a security case | case cards (Security Lab, Dashboard, Workspace welcome) | **Audit target** → Security case || yes |
| S2 | Import a public GitHub repository at a pinned commit; audit selected files | RepoImport panel | **Audit target** → GitHub repository || yes: URL / ref / subdir form |
| S3 | Audit the current Workspace design | ProjectAudit panel | **Audit target** → Workspace design || yes: design picker + Audit this design |
| S4 | List of all audits, open one | table + sidebar + Dashboard | audit list under the target picker (one list) || yes |
| S5 | Audit header: title, status, other audits of the case, report export | hero | compact header || yes: other-audit picker, Report export |
| S6 | Run again; repair with Factory Droid / model / reference (credit confirmation) | action row | header actions (Factory Droid primary, others in "More") || yes: Factory Droid primary; model and reference under More (not clicked: paid / writes) |
| S7 | Live progress while running | CurrentAction | CurrentAction in the header || yes (CurrentAction while running) |
| S8 | Pipeline stage cards, evidence ladder, stat tiles | Security Lab + Dashboard | removed as duplicates; the same facts are in the four blocks || removed duplicates; facts present in the four blocks |
| S9 | Audit target details: investigator, contract, functional contract, interface, board mapping, repository scope | contract / scope cards | block 1 **Audit target** (details expandable) || yes: contract, scope, agent review expand |
| S10 | Findings with source excerpt, permalink, hypothesis, history, dropped claims, missed failures | Vulnerability card + AI analysis + Findings card | block 2 **Findings** (details expandable) || yes: source, history, dropped claims, missed failures |
| S11 | Repair attempts: agent, diff (unified/split, copy), agent text, session, refused actions, TASK.md / trace | Repair summary + repair cards | block 3 **Repair** (all attempts, details expandable) || yes: diff unified/split/copy, agent trace files, other attempts with retests |
| S12 | Trusted tests on original and patch: per-test results, waveform per test, commands, tool versions, hashes | reproduction card, retest tables (twice), AI analysis waveform | block 4 **Verification** (one place, expandable) || yes: both test tables, per-test waveform, commands, versions, hashes |
| S13 | Formal: counterexample table on original, proof on patch, command | formal card + tiles | block 4 **Verification** || yes: counterexample and proof |
| S14 | Evidence record: artifacts with hashes, open report.md / audit.json / logs | evidence card | block 4 **Verification** → Evidence record || yes: 29 artifacts, file viewer |
| S15 | Test on FPGA | "Open in FPGA Lab" | **Test on FPGA** action (opens FPGA Lab on this audit) || yes: opens FPGA Lab on the audit |
| S16 | Limitations | card | expandable note at the bottom || yes |

## SiliconBench

| # | Capability | Before | After | Verified |
|---|---|---|---|---|
| B1 | Choose free / paid arms | arm groups | **Configuration** || yes: 7 arms |
| B2 | Worst-case estimate and payment confirmation; live-AI gating | estimate + confirm | unchanged (estimate shown next to Run) || yes: cost under the Run button, confirm unchanged (paid arms not run) |
| B3 | Run benchmark, pick an earlier run | header | header || yes |
| B4 | Metrics and per-arm comparison | rings + bars | **Results** || yes |
| B5 | Per-case results: harness validity, detection, repair, case classification, provenance, reproduce command, tool versions | case cards | **Results** table; provenance, command and versions under **Case details** || yes: table + Case details + tool versions |

## FPGA Lab

| # | Capability | Before | After | Verified |
|---|---|---|---|---|
| F1 | Board connection, detect | status strip + Connection view | header: board status + Detect; full connection details under Technical details || yes: Detect in header; full connection under Technical details (old Connection link opens it expanded) |
| F2 | What is on the board now (last program) | status strip | header line || yes: names the design |
| F3 | Security demo: audit picker, before/after variant | twin + HardwareDemo | step 1 **Bitstream** || yes |
| F4 | Simulate + build per variant | HardwareDemo | step 1 || yes (not clicked: starts Vivado) |
| F5 | Approval-gated programming per variant | HardwareDemo | step 1 || yes (disabled until the board is detected; asks for approval) |
| F6 | One Basys 3 twin, 2D/3D, demo steps set switches/button, LEDs from contract / simulation / webcam | twin | step 2 **Test** || yes: 2D/3D, steps, LED sources |
| F7 | Webcam verification (frame LEDs, capture steps, finish), expected vs observed, snapshots | HardwareDemo → BoardCamera | step 3 **Camera verification** || yes: session, stream, 5 capture steps on the scratch copy (not finished: no evidence written); expected vs observed table |
| F8 | Board-level simulation table, Vivado summary, hashes, programming log, pin mapping | HardwareDemo | Technical details || yes |
| F9 | Workspace design on the board: preview, simulation replay, virtual FPGA, live states, guide, program | Workspace hardware sidebar + FPGA Lab "Design board" | FPGA Lab target **Workspace design** (the one board) || yes: preview, simulation replay, virtual FPGA, live, guide |
| F10 | Design camera verification (phases, verdict) | Workspace Camera tab | FPGA Lab target **Workspace design** → Camera verification || yes: design camera under the inspector |
| F11 | Connection details: adapters, USB, xvcd (stop with approval), last program raw output, program a revision | FPGA Lab Connection view | Technical details → Connection || yes |

## Workspace

| # | Capability | Before | After | Verified |
|---|---|---|---|---|
| W1 | Conversational builder: new design, follow-ups, history, run options, approvals in chat | chat panel | **AI Builder** || yes: composer, run options, approvals in chat (nothing sent) |
| W2 | Agent timeline / current action of the run | Agent tab | **AI Builder** (beside the chat) || yes |
| W3 | Project selector, new design | chat header | compact selector in the Workspace header || yes |
| W4 | Revision selector | header | compact selector in the Workspace header || yes |
| W5 | Verilog files, highlighting, diff vs earlier revision | Code tab | **Code** || yes: 7 files, highlighting, diff |
| W6 | Diagram | Diagram tab | **Code** → Diagram toggle || yes: Diagram toggle; `#tab=diagram` opens it |
| W7 | Simulation results and full waveform viewer | Simulation tab | **Simulation** || yes: waveform with zoom / format / fit |
| W8 | Vivado synthesis, implementation, timing, bitstream, reports, logs, rebuild | Vivado tab | **Build** action (panel) || yes: stages, timing, reports, logs, rebuild (not clicked) |
| W9 | Revision history, compare revisions, revision changes | Revisions tab | **History** action (panel); compare opens Code diff || yes: changes, projects, revisions; Compare opens Code r1→r2 diff |
| W10 | Hardware sidebar | Workspace right panel | FPGA Lab (F9) || moved: F9 |
| W11 | Camera tab | Workspace | FPGA Lab (F10) || moved: F10 |
| W12 | Welcome page: example prompts, readiness | Workspace empty state | AI Builder empty state || yes: 4 example prompts fill the composer |
| W13 | Stage links from the timeline (open Code / Simulation / Build / FPGA) | tab links | same links, mapped to the new views || yes: "Open Build →" opens the Build panel |
| W14 | Presentation mode | top bar | top bar || yes |
