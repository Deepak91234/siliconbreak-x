# CircuitLoop — autonomous AI FPGA engineer for the Basys 3

CircuitLoop turns a natural-language circuit description into a verified
Digilent Basys 3 design. It writes the specification, generates Verilog, writes
independent tests, simulates with Verilator, repairs failures (at most 3
automatic attempts), builds with Vivado in your existing Docker installation,
asks before programming the board, programs it, and checks the LEDs with your
webcam. A live dashboard shows each step as it happens.

Every status in the dashboard comes from a real tool run: the LLM, Verilator,
Vivado, openFPGALoader or hw_server, or OpenCV. When a tool or the board is
missing, the stage says **unavailable**; it never reports a pass.

## Quick start

```bash
cd ~/Desktop/CircuitLoop
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # add ANTHROPIC_API_KEY for the LLM agent (optional)
(cd frontend && npm install)
./scripts/dev.sh                # backend :8000 + dashboard http://127.0.0.1:5173
```

Required host tools (Homebrew): `verilator` (lint and simulation) and
`openfpgaloader` (native board detection and programming). Docker Desktop must
be running for Vivado builds.

### Vivado

CircuitLoop uses your existing **cse100-vivado-on-silicon-mac** installation
(`~/cse100/cse100-vivado-on-silicon-mac-main`). It never installs a second
Vivado and never changes your Docker configuration.

* If `vivado_container` is running (started by your usual
  `scripts/start_container.sh`), CircuitLoop reuses it. It only runs
  `docker exec … vivado -mode batch`, and your GUI session keeps running.
* If no container is running, the dashboard offers to start a separate
  **headless** container, `circuitloop_vivado`. It uses the same image
  (`x64-linux`) and the same bind mount, but no VNC, no Screen Sharing and no
  xvcd. It starts only after you approve it, and CircuitLoop only stops
  containers labelled `circuitloop.managed=true`.
* Builds run in `~/cse100/cse100-vivado-on-silicon-mac-main/circuitloop_ws/builds/<id>`,
  which the container sees as `/home/user/circuitloop_ws/…`. Results are copied
  into `runs/projects/<project>/<revision>/vivado/`.
* Only one CircuitLoop build runs at a time, enforced by a process lock plus a
  file lock in the shared workspace.

You can still start the Vivado GUI the usual way:

```bash
~/cse100/cse100-vivado-on-silicon-mac-main/scripts/start_container.sh
```

### Board programming and the XVC conflict

`start_container.sh` runs the macOS `xvcd` USB bridge in a loop while the GUI
container is up. `xvcd` holds the Basys 3's FTDI JTAG interface, so
openFPGALoader cannot open the board while it runs. CircuitLoop detects this
and picks an adapter:

* **openFPGALoader** (`openFPGALoader -b basys3 design.bit`, volatile SRAM
  only): used when the board is visible on USB and `xvcd` is not holding it.
* **Vivado hw_server**: used when `xvcd` and `vivado_container` are running. It
  programs through the existing XVC bridge without disrupting it.

Programming always needs an explicit approval in the dashboard. The approval
is bound to the SHA-256 of the exact bitstream. CircuitLoop never writes the
configuration flash.

## Layout

```
backend/   FastAPI app (api/), orchestrator + LLM agent (agent/), spec IR (specifications/),
           trusted RTL generators + safety checks (rtl/), Verilator oracle (verification/),
           Vivado integration (vivado/), board programming (programmer/), webcam (vision/),
           SQLite persistence (storage/)
boards/basys3/   trusted pin map (board.json) + official Digilent master XDC
rtl/library/     preverified Verilog: synchronizer, debouncer, tick divider, edge detector
frontend/        React + TypeScript + Vite + Tailwind dashboard
docs/            contracts.md (module interfaces, normative semantics), api.md (HTTP/WS API)
runs/            run artifacts: RTL, reports, bitstreams, waveforms, webcam evidence, SQLite DB
scripts/         dev.sh, phase1_pipeline.py (CLI proof of the toolchain), llm_smoke.py
tests/           pytest suite (Vivado/hardware tests are opt-in: --run-vivado / --run-hardware)
```

## Tests

```bash
.venv/bin/python -m pytest -q                  # unit + Verilator tests
.venv/bin/python -m pytest -q --run-vivado     # also runs a real Vivado build (about 2-3 min)
.venv/bin/python scripts/phase1_pipeline.py    # CLI: spec -> Verilator -> Vivado -> .bit
```
