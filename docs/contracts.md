# SiliconBreak X internal module contracts

This file fixes the Python interfaces between subsystems so they can be built and
tested independently. **Signatures here are binding**; implementations may add
helpers but must not change these names, arguments or result shapes without
updating this file and every caller.

Already implemented and verified on the real machine (Phase 1):

| Module | Purpose |
|---|---|
| `backend/specifications/ir.py` | `DesignSpec` IR (families: counter, blinker, led_pattern, traffic_light) |
| `backend/specifications/timing.py` | `timing_params(spec, clk_hz)`, `param_dict(spec, clk_hz)`, `timing_problems(spec, clk_hz)` |
| `backend/specifications/derive.py` | `derive_requirements(spec)`, `derive_observations(spec)` (shared by oracle + vision) |
| `backend/specifications/board.py` | `load_board("basys3")` trusted pin map |
| `backend/rtl/wrapper.py` | `generate_wrapper(spec)`, `generate_xdc(spec)`, `core_interface(spec)`, `core_ports(spec)`, `library_paths()` |
| `backend/rtl/templates.py` | `generate_core(spec)` deterministic cores (counter done; others TODO) |
| `backend/verification/verilator.py` | `lint`, `build_harness`, `run_harness`, `parse_trace` (real Verilator 5.052) |
| `backend/verification/harness/tb_main.cpp` | generic stimulus/trace harness for `basys3_top` |
| `backend/vivado/container.py` | `detect()`, `start_headless()`, `stop_headless()`, `start_docker_desktop()`, `container_exec()` |
| `backend/vivado/build.py` | `run_build(build_id, sources, xdc, artifacts_dir, on_line=, on_stage=, cancel=)` |
| `backend/vivado/reports.py` | utilization / timing / DRC / bitstream-header parsers |
| `backend/core/process.py` | `run_command(argv, ...)` (argv only, never a shell), `ensure_within(path, root)` |
| `backend/core/events.py` | `bus` (EventBus), `Event`, `STAGES`, `STAGE_KEYS`, `STAGE_STATUSES` |
| `backend/core/config.py` | `get_settings()` |

## Design hierarchy (every design)

```
basys3_top  (trusted generator; fixed board ports; parameters CLK_HZ, TICKS_*, DEBOUNCE_*)
 ├─ cl_input_cond × N   (library: 2-FF sync + debounce; one per InputBinding)
 ├─ cl_core             (behaviour; template generator or LLM; must match core_interface(spec) exactly)
 └─ unused LEDs = 0, seven-segment blanked
```

* `cl_core` receives **clean, synchronized, debounced, active-high** inputs.
* Timing constants are parameters (`TICKS_<MODE>`, `TICKS_STATE_<STATE>`), so simulation
  overrides them with `-G` for an accelerated clock (default `SILICONBREAK_SIM_CLK_HZ=10000`).
* Library modules usable inside `cl_core`: `cl_tick_div` (clock-enable generator),
  `cl_edge_rise` (rising-edge pulse), `cl_sync2`, `cl_debounce`, `cl_input_cond`.
* Trace/stimulus formats: see the header of `backend/verification/harness/tb_main.cpp`.
  Cycle k = k-th rising edge; inputs listed at cycle k are applied just before edge k;
  trace values at cycle k are sampled just after edge k; cycle 0 = initial values.

## Behavioural semantics the oracle must check (normative)

These come from the `ir.py` docstrings; restated precisely:

* **counter**: value starts at `initial_value`. Each tick of the active rate mode
  adds +1 (direction up) / −1 (down) modulo 2^width (`wrap=True`) or saturates.
  Active mode = first `rate_modes` entry whose `when_input` equals `when_level`;
  the last mode is the unconditional default. Interval between steps in a mode
  is `round(CLK_HZ / hz)` cycles. While `reset_input` is high the value is
  `reset_value` and stays there; the step timer restarts, so the first step after
  release comes one full period later (±small latency). While `enable_input` is
  low the counter does not step. `direction_control.input` high means
  `high_means` direction.
* **blinker**: LEDs toggle every `round(CLK_HZ / (2*hz))` cycles (50% duty).
  `together`: all output bits equal. `alternate`: even bits = ¬odd bits.
  While `enable_input` is low all output bits are 0. Initial state: all bits 0
  for `together`; for `alternate` bit0=0, bit1=1, …
* **led_pattern**:
  `mirror_inputs` out[i] = source_inputs[i]; `invert_inputs` out[i] = ¬source_inputs[i]
  (combinational after conditioning; latency ≤ a few cycles + debounce).
  `chase`: exactly one bit set, initially bit 0; each step moves one position
  (`left` = towards higher index), wrapping.
  `bounce`: exactly one bit set, initially bit 0, moving left until the top bit,
  then right until bit 0, and so on (the end bits are lit for one step each).
  `fill`: initially 0; each step sets the next bit (left: bit0 first); after all
  bits are set the next step clears to 0.
  `enable_input` low → hold. `reset_input` high → initial pattern, timer restarted.
  `direction_control` overrides `direction` while the input is active.
* **traffic_light**: start in `states[0]`, timer at 0. Output = `lights` of the
  current state (bit i → output bit i; width = output width). After
  `round(CLK_HZ * duration_s)` cycles move to the next state (wrap). If
  `request_input` is set: a press (rising edge of the conditioned input) is
  latched; when `request_after` ends with the latch set, go to `request_state`
  (for its duration), clear the latch, then continue with the state after
  `request_after`. `reset_input` high → `states[0]` with timer restarted, latch cleared.
* Every unused LED (`R-IDLE`) is 0 at every sampled cycle.

Tolerances: an interval check passes if |measured − expected| ≤ max(4 cycles, 0.2% of expected).
Intervals that contain an input change (plus the input's debounce + 4 cycle latency window)
are excluded from steady-state rate checks and checked only for "no illegal value".

## A. Simulation oracle — `backend/verification/`

```python
# testgen.py
class StimulusEvent(BaseModel): cycle: int; port: Literal["sw","btnC","btnU","btnL","btnR","btnD"]; value: int
class CheckSpec(BaseModel):   id: str; kind: str; requirement_ids: list[str]; params: dict = {}
class Scenario(BaseModel):
    id: str; title: str; description: str
    requirement_ids: list[str]
    events: list[StimulusEvent]
    end_cycle: int
    vcd_window: tuple[int, int] | None
    checks: list[CheckSpec]
class TestPlan(BaseModel):
    spec_title: str; clk_hz: int; params: dict[str, int]; scenarios: list[Scenario]
def generate_test_plan(spec: DesignSpec, clk_hz: int) -> TestPlan

# oracle.py  (independent of the RTL: derives expectations from the spec only)
class CheckResult(BaseModel):
    id: str; scenario_id: str; requirement_ids: list[str]; passed: bool
    summary: str                       # one line, human readable
    expected: Any = None; observed: Any = None
    first_failure_cycle: int | None = None
    details: dict = {}
def evaluate_scenario(spec: DesignSpec, plan: TestPlan, scenario: Scenario, trace: Trace) -> list[CheckResult]

# runner.py
class SimulationReport(BaseModel):
    status: Literal["passed","failed","error","unavailable"]
    lint: dict            # {"ok": bool, "messages": [LintMessage.as_dict()], "params": "real"|"sim"} for both param sets
    build: dict           # {"ok": bool, "duration_s": float, "log_tail": str}
    plan: TestPlan | None
    scenarios: list[dict] # [{"id","title","passed","checks":[CheckResult], "trace_file", "vcd_file", "duration_s"}]
    requirements: list[dict]   # [{"id","text","methods","sim_status": "passed"|"failed"|"not_tested"}]
    passed_checks: int; failed_checks: int
    diagnostics: dict     # compact failure digest for the repair prompt (failing checks, lint errors, trace excerpts)
    duration_s: float
async def run_simulation_suite(
    spec: DesignSpec, rtl_files: list[Path], workspace: Path,
    emit: Callable[[str, dict], Awaitable[None]] | None = None,   # emit("log", {"line": ...}) / emit("scenario", {...})
    clk_hz: int | None = None,                                      # default get_settings().sim_clk_hz
    *, fail_fast: bool = False, waveforms: bool = True,             # used by the mutation tests
) -> SimulationReport
# report.lint is keyed "real" and "sim"; a design compile error in the harness build gives
# status "failed" with diagnostics.build_errors (and empty lint_errors).
# rtl_files = [basys3_top.v, cl_core.v]; runner adds library_paths(). Lint runs at real AND sim params.

# waveform.py
def vcd_to_json(vcd: Path, signals: list[str] | None = None, start: int | None = None,
                end: int | None = None, max_changes_per_signal: int = 4000) -> dict
# -> {"timescale": "...", "time_unit": "half_cycle", "start": int, "end": int,
#     "signals": [{"name": "TOP.basys3_top.u_core.value", "width": 4, "changes": [[t, "0x3"], ...]}]}

# mutations.py  (deliberately defective RTL for tests and the fault-injection demo mode)
MUTATIONS: dict[str, Mutation]      # name -> description + applies_to(family) + apply(verilog_text) -> str
def apply_mutation(name: str, verilog: str) -> str     # raises ValueError if it does not apply
```

## B. AI agent — `backend/agent/`, `backend/specifications/validate.py`, `backend/rtl/safety.py`

```python
# agent/llm/base.py
class LLMResult(BaseModel, Generic[T]):
    value: T | None; text: str | None; model: str; provider: str
    stop_reason: str | None; usage: dict; duration_s: float; request_id: str | None
class LLMError(Exception): ...          # .kind in {"auth","rate_limit","refusal","invalid_output","network","api"}
class LLMProvider(Protocol):
    name: str; model: str
    async def structured(self, *, purpose: str, system: str, messages: list[dict],
                         output_model: type[T], max_tokens: int = 16000) -> LLMResult[T]: ...
# agent/llm/anthropic_provider.py : AnthropicProvider(api_key, model)  (AsyncAnthropic; key never logged)
# agent/llm/__init__.py : def get_provider(settings=None) -> LLMProvider | None   (None when not configured)

# agent/intent.py
class IntentResult(BaseModel):
    kind: Literal["new_design", "modify", "question", "unsupported", "clarify"]
    spec: DesignSpec | None
    message: str              # what the agent tells the user (plain language)
    change_summary: list[str] # for modify: concrete spec changes
    assumptions: list[str]
    unsupported_reasons: list[str]
    interpreter: str          # "llm:<model>" | "offline-rules"
async def interpret_request(text: str, current: DesignSpec | None, provider: LLMProvider | None) -> IntentResult
# offline-rules fallback (provider None) must at least handle Demo 1 and Demo 2 phrasing and simple
# blinker / counter / traffic-light requests, and say clearly when it cannot.

# specifications/validate.py
class ValidationIssue(BaseModel): severity: Literal["error","warning","info"]; code: str; message: str; path: str | None = None
class ValidationReport(BaseModel): ok: bool; issues: list[ValidationIssue]
def validate_spec(spec: DesignSpec) -> ValidationReport

# agent/rtlgen.py
class RtlGeneration(BaseModel):
    verilog: str              # complete `module cl_core ... endmodule` source
    generator: str            # "llm:<model>" | "template/<family>"
    explanation: str
    safety: list[dict]        # issues from rtl/safety.check_core_source
async def generate_core_rtl(spec: DesignSpec, provider: LLMProvider | None,
                            previous_verilog: str | None = None, change_request: str | None = None) -> RtlGeneration
async def repair_core_rtl(spec: DesignSpec, verilog: str, diagnostics: dict, attempt: int,
                          provider: LLMProvider | None) -> RtlGeneration
# provider None -> template generator (generate_core), repair raises RepairUnavailable.

# rtl/safety.py
class SafetyIssue(BaseModel): severity: Literal["error","warning"]; code: str; message: str; line: int | None = None
def check_core_source(verilog: str, spec: DesignSpec) -> list[SafetyIssue]
# errors: system tasks ($system/$fopen/$fwrite/$readmem*/$finish/...), `include/`define, DPI, extra modules,
# module name != cl_core, port list mismatch vs core_ports(spec), clocks other than clk
# (always @(posedge X) with X != clk), initial blocks, latches via always @*, oversize/non-ASCII.
```

## C. Programmer — `backend/programmer/`

```python
class UsbDevice(BaseModel): vendor_id: int; product_id: int; product: str | None; serial: str | None; location: str | None
class XvcStatus(BaseModel):
    xvcd_pids: list[int]; launcher_running: bool; port_2542_listeners: list[dict]; conflict: bool; note: str
class DetectResult(BaseModel):
    adapter: Literal["openfpgaloader", "hw_server"]; ok: bool
    state: Literal["connected","no_board","busy","unsupported","error","tool_missing"]
    idcode: str | None; device: str | None; message: str; raw: str; duration_s: float
class ProgramResult(BaseModel):
    adapter: str; ok: bool; state: Literal["programmed","failed","busy","no_board","refused"]
    bitstream_path: str; bitstream_sha256: str; message: str; raw: str; duration_s: float; finished_at: float
class HardwareStatus(BaseModel):
    state: Literal["connected","no_board","busy","unsupported","error","tool_missing","unknown"]
    board: dict                       # {"name","part","idcode"} from board.json
    usb_devices: list[UsbDevice]; xvc: XvcStatus
    adapters: dict                    # {"openfpgaloader": {"available","path","version"}, "hw_server": {"available","reason"}}
    preferred_adapter: str | None
    last_detect: DetectResult | None; last_program: ProgramResult | None
    programming: bool; checked_at: float; message: str

class HardwareManager:
    async def status(self, deep: bool = False) -> HardwareStatus   # deep=True runs JTAG detect; False = USB/xvc only
    async def detect(self) -> DetectResult
    async def program(self, bitstream: Path, sha256: str, *, approval_id: str) -> ProgramResult
        # verifies sha256 of the file == sha256 (refuses otherwise), uses volatile SRAM programming only,
        # never writes flash, single programming at a time
    async def stop_xvcd(self, pid: int, *, approval_id: str) -> dict   # only kills a verified xvcd process
    async def monitor(self, interval_s: float = 3.0) -> None          # USB/xvc polling; publishes bus "hardware.updated" on change
hardware = HardwareManager()   # module-level singleton in programmer/manager.py
```

## D. Vision — `backend/vision/`

```python
class LedRoi(BaseModel): led_index: int; x: float; y: float; w: float; h: float   # normalized 0..1 of frame
class RoiConfig(BaseModel): leds: list[LedRoi]
class FrameResult(BaseModel):
    t_ms: float; states: dict[int, int | None]; levels: dict[int, float]; quality: dict
class PhasePlan(BaseModel):
    id: str; observation_id: str | None; title: str; instruction: str
    min_duration_s: float; conditions: list[dict]
class VerificationResult(BaseModel):
    status: Literal["passed","failed","inconclusive","unavailable"]
    summary: str
    observations: list[dict]   # per observation: status, measured, expected, tolerance, n_events, ci, notes
    requirements: list[dict]   # [{"id","text","physical_status": "observed_pass"|"observed_fail"|"inconclusive"|"not_observable"|"not_observed"}]
    quality: dict
    evidence: list[dict]       # [{"name","path","kind": "plot"|"csv"|"json"|"snapshot"}]
class VisionSession:
    id: str
    def __init__(self, session_id: str, spec: DesignSpec, evidence_dir: Path): ...
    plan: list[PhasePlan]                      # derived from derive_observations(spec)
    def set_roi(self, roi: RoiConfig) -> None
    def auto_roi(self, jpeg: bytes, box: tuple[float,float,float,float], count: int, first_led: int) -> RoiConfig
    def ingest_frame(self, t_ms: float, jpeg: bytes) -> FrameResult
    def start_phase(self, phase_id: str) -> PhasePlan
    def end_phase(self) -> dict                # returns phase measurement summary
    def finish(self) -> VerificationResult     # writes evidence files
    def to_dict(self) -> dict                  # JSON-safe state for the API (plan, current phase, roi, live stats)
```

## E. Orchestrator / storage / API

See `docs/api.md` for the HTTP/WebSocket contract the frontend uses.
