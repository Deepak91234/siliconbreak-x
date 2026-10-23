/**
 * Wire types for the CircuitLoop backend, transcribed from docs/api.md and
 * docs/contracts.md. They describe what the server sends; the UI never fills
 * these in itself. Fields the contract leaves open are typed as `unknown` /
 * index signatures so unexpected data is rendered generically instead of being
 * silently dropped.
 */

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Dict = Record<string, unknown>;

// ---------------------------------------------------------------- vocabularies

export const STAGE_KEYS = [
  "spec", "validate", "rtl", "lint", "tests", "simulate", "repair",
  "synth", "impl", "timing", "bitstream", "detect", "program", "verify",
] as const;
export type StageKey = (typeof STAGE_KEYS)[number];

export const STAGE_STATUSES = [
  "pending", "running", "passed", "failed", "skipped", "blocked", "unavailable", "inconclusive", "cancelled",
] as const;
export type StageStatus = (typeof STAGE_STATUSES)[number];

export const RUN_STATUSES = ["running", "waiting", "passed", "passed_sim_only", "failed", "cancelled", "unsupported"] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export const APPROVAL_STATUSES = ["pending", "approved", "denied", "executed", "failed", "expired"] as const;
export type ApprovalStatus = (typeof APPROVAL_STATUSES)[number];

export const HARDWARE_STATES = ["connected", "no_board", "busy", "unsupported", "error", "tool_missing", "unknown"] as const;
export type HardwareState = (typeof HARDWARE_STATES)[number];

export const VISION_VERDICTS = ["passed", "failed", "inconclusive", "unavailable"] as const;
export type VisionVerdict = (typeof VISION_VERDICTS)[number];

export type RevisionStatus =
  | "draft" | "lint_failed" | "sim_failed" | "sim_passed" | "build_failed"
  | "built" | "programmed" | "verified" | "verify_failed";

export type PhysicalStatus = "observed_pass" | "observed_fail" | "inconclusive" | "not_observable" | "not_observed";
export type SimRequirementStatus = "passed" | "failed" | "not_tested";

// ---------------------------------------------------------------- core objects

export interface Stage {
  key: StageKey | string;
  label: string;
  status: StageStatus;
  summary: string | null;
  started_at: number | null;
  finished_at: number | null;
  detail: Dict;
}

export interface RunOptions {
  inject_fault: string | null;
  simulation_only: boolean;
}

export type RunTrigger = "new" | "modify" | "rebuild" | "reprogram";

export interface Run {
  id: string;
  project_id: string;
  trigger: RunTrigger | string;
  request_text: string;
  status: RunStatus;
  revision_id: string | null;
  revision_ids: string[];
  stages: Stage[];
  options: RunOptions;
  error: string | null;
  created_at: number;
  finished_at: number | null;
}

export interface Project {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  current_revision_id: string | null;
  revision_count: number;
  latest_run_id: string | null;
  latest_run_status: RunStatus | null;
}

export type MessageRole = "user" | "agent" | "system";
export type MessageKind = "text" | "spec" | "approval" | "error" | "result";

export interface Message {
  id: string;
  project_id: string;
  run_id: string | null;
  role: MessageRole;
  kind: MessageKind | string;
  text: string;
  data: Dict;
  created_at: number;
}

export type RevisionKind = "initial" | "modification" | "repair";

export interface RevisionSummary {
  id: string;
  project_id: string;
  number: number;
  parent_id: string | null;
  kind: RevisionKind | string;
  user_request: string;
  generator: string;
  interpreter: string;
  status: RevisionStatus | string;
  sim_status: "passed" | "failed" | null;
  build_status: "passed" | "failed" | "unavailable" | null;
  change_summary: string[];
  created_at: number;
}

export interface RevisionFile {
  name: string;
  role: "core" | "wrapper" | "xdc" | "library" | string;
  sha256: string;
  size: number;
}

/** derive.py Requirement */
export interface Requirement {
  id: string;
  text: string;
  category: string;
  methods: Array<"simulation" | "physical">;
  physical_note?: string | null;
}

/** derive.py Condition */
export interface Condition {
  input: string;
  board_signal: string;
  level: 0 | 1;
  instruction: string;
}

/** derive.py Observation */
export interface Observation {
  id: string;
  requirement_ids: string[];
  kind: string;
  description: string;
  leds: number[];
  conditions: Condition[];
  expected: Dict;
  min_duration_s: number;
  camera_verifiable: boolean;
  reason_not_verifiable: string | null;
}

export interface SimulationSummary {
  status: string;
  passed_checks: number;
  failed_checks: number;
  scenarios: Array<{ id: string; title: string; passed: boolean }>;
}

export interface BuildSummary {
  status: string;
  timing: TimingSummary | null;
  utilization: Utilization | null;
  bitstream_sha256: string | null;
  bitstream_size: number | null;
  checks: BuildCheck[];
}

export interface Revision extends RevisionSummary {
  spec: DesignSpec;
  requirements: Requirement[];
  observations: Observation[];
  files: RevisionFile[];
  safety_issues: Array<Dict>;
  explanation: string;
  simulation: SimulationSummary | null;
  build: BuildSummary | null;
  verification: VerificationResult | null;
}

export type ApprovalKind = "program_fpga" | "start_vivado_container" | "start_docker" | "stop_xvcd";

export interface Approval {
  id: string;
  kind: ApprovalKind | string;
  status: ApprovalStatus;
  title: string;
  description: string;
  details: Dict;
  run_id: string | null;
  project_id: string | null;
  created_at: number;
  decided_at: number | null;
  result: unknown;
}

// ---------------------------------------------------------------- DesignSpec (ir.py)

export interface InputBinding {
  name: string;
  port: "btnC" | "btnU" | "btnL" | "btnR" | "btnD" | "sw";
  index?: number | null;
  debounce_ms: number;
  description?: string;
}

export interface OutputBinding {
  name: string;
  port: "led";
  indices: number[];
  description?: string;
}

export interface RateMode {
  name: string;
  hz: number;
  when_input?: string | null;
  when_level?: 0 | 1;
}

export interface DesignSpec {
  spec_version: string;
  title: string;
  summary: string;
  board: string;
  clock_hz: number;
  inputs: InputBinding[];
  outputs: OutputBinding[];
  behavior: { family: string; output: string; rate_modes?: RateMode[]; [k: string]: unknown };
  assumptions: string[];
}

// ---------------------------------------------------------------- simulation (contracts §A)

export interface LintMessage {
  severity: string;
  code: string | null;
  file: string | null;
  line: number | null;
  column: number | null;
  message: string;
  fatal: boolean;
  context: string[];
}

export interface LintBlock {
  ok: boolean;
  messages: LintMessage[];
  params?: "real" | "sim" | string;
}

export interface StimulusEvent { cycle: number; port: string; value: number }
export interface CheckSpec { id: string; kind: string; requirement_ids: string[]; params: Dict }

export interface Scenario {
  id: string;
  title: string;
  description: string;
  requirement_ids: string[];
  events: StimulusEvent[];
  end_cycle: number;
  vcd_window: [number, number] | null;
  checks: CheckSpec[];
}

export interface TestPlan {
  spec_title: string;
  clk_hz: number;
  params: Record<string, number>;
  scenarios: Scenario[];
}

export interface CheckResult {
  id: string;
  scenario_id: string;
  requirement_ids: string[];
  passed: boolean;
  summary: string;
  expected?: unknown;
  observed?: unknown;
  first_failure_cycle?: number | null;
  details?: Dict;
}

export interface ScenarioResult {
  id: string;
  title: string;
  passed: boolean;
  checks: CheckResult[];
  trace_file?: string | null;
  vcd_file?: string | null;
  duration_s?: number | null;
}

export interface SimulationReport {
  status: "passed" | "failed" | "error" | "unavailable";
  /** Either a single LintBlock or a map of parameter-set name -> LintBlock. */
  lint: LintBlock | Record<string, LintBlock> | Dict;
  build: { ok: boolean; duration_s: number; log_tail: string } | Dict;
  plan: TestPlan | null;
  scenarios: ScenarioResult[];
  requirements: Array<{ id: string; text: string; methods: string[]; sim_status: SimRequirementStatus }>;
  passed_checks: number;
  failed_checks: number;
  diagnostics: Dict;
  duration_s: number;
}

export interface WaveSignal {
  name: string;
  width: number;
  changes: Array<[number, string]>;
}

export interface Waveform {
  timescale: string;
  time_unit: string;
  start: number;
  end: number;
  signals: WaveSignal[];
}

// ---------------------------------------------------------------- Vivado

export interface TimingSummary {
  wns_ns: number | null;
  tns_ns: number | null;
  tns_failing_endpoints: number | null;
  whs_ns: number | null;
  ths_ns: number | null;
  ths_failing_endpoints: number | null;
  wpws_ns: number | null;
  total_endpoints: number | null;
  constraints_met: boolean | null;
  clocks: Array<{ name: string; period_ns: number | null; freq_mhz: number | null }>;
  unconstrained_note: string | null;
  timing_met: boolean;
}

export interface UtilResource { name: string; key: string | null; used: number; available: number; percent: number }
export interface Utilization {
  resources: UtilResource[];
  summary: Record<string, { used: number; available: number; percent: number }>;
}

export interface DrcSummary {
  checks_found: number | null;
  errors: number;
  critical_warnings: number;
  warnings: number;
  violations: Array<{ rule: string; severity: string; description: string; count: number }>;
}

export interface BitstreamInfo {
  valid: boolean;
  size_bytes: number;
  design_field: string | null;
  part: string | null;
  date: string | null;
  time: string | null;
  data_length: number | null;
  sync_word_found: boolean;
  problem: string | null;
}

export interface BuildCheck { name: string; passed: boolean; detail?: string }

export interface VivadoBuildResult {
  build_id: string;
  ok: boolean;
  status: string;
  failed_stage: string | null;
  message: string;
  container: string | null;
  vivado_version: string | null;
  duration_s: number;
  exit_code: number | null;
  stage_times: Record<string, number>;
  timing: TimingSummary | null;
  utilization: Utilization | null;
  utilization_synth: Utilization | null;
  drc: DrcSummary | null;
  log_summary: { critical_warnings: string[]; errors: string[]; warning_count: number; warnings_sample: string[] } | null;
  tcl_metrics: Dict | null;
  bitstream_path: string | null;
  bitstream_sha256: string | null;
  bitstream: BitstreamInfo | null;
  artifacts_dir: string | null;
  workspace_dir_host: string | null;
  checks: BuildCheck[];
}

export const REPORT_NAMES = ["timing_summary.rpt", "utilization_impl.rpt", "drc.rpt", "power.rpt", "io.rpt"] as const;

// ---------------------------------------------------------------- hardware (contracts §C)

export interface UsbDevice { vendor_id: number; product_id: number; product: string | null; serial: string | null; location: string | null }

export interface XvcStatus {
  xvcd_pids: number[];
  launcher_running: boolean;
  port_2542_listeners: Dict[];
  conflict: boolean;
  note: string;
}

export interface DetectResult {
  adapter: string;
  ok: boolean;
  state: string;
  idcode: string | null;
  device: string | null;
  message: string;
  raw: string;
  duration_s: number;
}

export interface ProgramResult {
  adapter: string;
  ok: boolean;
  state: "programmed" | "failed" | "busy" | "no_board" | "refused" | string;
  bitstream_path: string;
  bitstream_sha256: string;
  message: string;
  raw: string;
  duration_s: number;
  finished_at: number;
}

export interface HardwareStatus {
  state: HardwareState;
  board: { name?: string; part?: string; idcode?: string; [k: string]: unknown };
  usb_devices: UsbDevice[];
  xvc: XvcStatus;
  adapters: Record<string, { available?: boolean; path?: string | null; version?: string | null; reason?: string | null; [k: string]: unknown }>;
  preferred_adapter: string | null;
  last_detect: DetectResult | null;
  last_program: ProgramResult | null;
  programming: boolean;
  checked_at: number;
  message: string;
}

// ---------------------------------------------------------------- environment

export interface ContainerInfo {
  name: string;
  exists: boolean;
  running: boolean;
  status: string | null;
  image: string | null;
  managed_by_circuitloop: boolean;
  home_mount_source: string | null;
  started_at: string | null;
}

export interface VivadoEnvironment {
  checked_at: number;
  docker_cli: string | null;
  docker_daemon: boolean;
  docker_server_version: string | null;
  docker_error: string | null;
  image_present: boolean;
  containers: ContainerInfo[];
  active_container: string | null;
  vivado_versions: string[];
  vivado_version: string | null;
  vivado_bin: string | null;
  settings_script: string | null;
  vivado_executable: boolean;
  gui_session_running: boolean;
  hw_server_running: boolean;
  batch_builds_running: number;
  workspace_host: string | null;
  workspace_container: string | null;
  workspace_shared: boolean;
  host_root_exists: boolean;
  can_start_headless: boolean;
  problems: string[];
  ready: boolean;
  build_running: boolean;
}

export interface ToolInfo { available: boolean; path: string | null; version: string | null }

export interface Environment {
  checked_at: number;
  vivado: VivadoEnvironment;
  verilator: ToolInfo;
  openfpgaloader: ToolInfo;
  llm: { provider: string; model: string; configured: boolean };
  hardware: HardwareStatus;
  config: { runs_dir?: string; sim_clk_hz?: number; max_repair_attempts?: number; [k: string]: unknown };
}

// ---------------------------------------------------------------- vision (contracts §D)

export interface LedRoi { led_index: number; x: number; y: number; w: number; h: number }
export interface RoiConfig { leds: LedRoi[] }

export interface PhasePlan {
  id: string;
  observation_id: string | null;
  title: string;
  instruction: string;
  min_duration_s: number;
  conditions: Array<Partial<Condition> & Dict>;
}

export interface VerificationResult {
  status: VisionVerdict;
  summary: string;
  observations: Array<Dict>;
  requirements: Array<{ id: string; text: string; physical_status: PhysicalStatus | string }>;
  quality: Dict;
  evidence: Array<{ name: string; path: string; kind: "plot" | "csv" | "json" | "snapshot" | string }>;
}

export interface VisionSessionState {
  id: string;
  revision_id: string;
  status: "setup" | "running" | "finished" | string;
  plan: PhasePlan[];
  current_phase: string | null;
  roi: RoiConfig | null;
  live: { fps?: number; frames?: number; quality?: Dict; [k: string]: unknown };
  result: VerificationResult | null;
  [k: string]: unknown;
}

export type VisionServerMessage =
  | { type: "frame"; t_ms: number; states: Record<string, number | null>; levels: Record<string, number>; quality: Dict }
  | { type: "state"; session: VisionSessionState }
  | { type: "error"; message: string };

// ---------------------------------------------------------------- events

export type EventType =
  | "run.created" | "run.updated" | "run.finished"
  | "stage.updated" | "log" | "agent.message"
  | "approval.requested" | "approval.resolved"
  | "revision.created" | "revision.updated"
  | "environment.updated" | "hardware.updated" | "vision.updated";

export interface CLEvent {
  seq: number;
  ts: number;
  type: EventType | string;
  run_id: string | null;
  project_id: string | null;
  stage: string | null;
  data: Dict;
}

// ---------------------------------------------------------------- endpoint payloads

export interface ProjectDetailResponse {
  project: Project;
  revisions: RevisionSummary[];
  runs: Run[];
  messages: Message[];
}

export interface DiffResponse {
  against: string | null;
  files: Array<{ name: string; diff: string }>;
  spec_diff: string;
}

/**
 * GET /api/faults (implemented by backend/api/routes.py, not yet listed in
 * docs/api.md): defects the fault-injection test mode can insert into cl_core.
 */
export interface FaultInfo {
  name: string;
  description: string;
  families: string[];
}

// ---------------------------------------------------------------- hardware guide (guide_routes.py)

export interface BoardDef {
  id: string;
  name: string;
  part: string;
  idcode: string;
  clock: { port: string; hz: number };
  iostandard: string;
  ports: Record<string, { direction: string; kind: string; pins: string[]; active: string | null }>;
}

export type GuideAction = "hold" | "press" | "release" | "set_on" | "set_off" | "observe";

export interface GuideComponent {
  id: string;
  kind: "button" | "switch" | "led";
  label: string;
  pin: string | null;
  role: "input" | "output";
  signal: string;
  function: string;
  debounce_ms?: number;
}

export interface GuideStep {
  id: string;
  title: string;
  instruction: string;
  actions: Array<{ component: string; label: string; action: GuideAction; signal: string }>;
  observe: string[];
  expected: string;
  confirm: string;
  requirement_ids: string[];
  scenario_id: string | null;
  observation_id: string | null;
  sim_status: "passed" | "failed" | "not_tested";
  hold: boolean;
  camera_verifiable: boolean;
}

export interface Guide {
  board: string;
  title: string;
  summary: string;
  family: string;
  components: GuideComponent[];
  unused_note: string;
  steps: GuideStep[];
  derived_from: { spec: boolean; test_scenarios: string[]; simulation_report: boolean };
  camera_limit_hz: number;
  preview: boolean;
  example?: string;
  revision_id: string | null;
  revision_number?: number;
  programmed_by_circuitloop?: boolean;
  sim_status?: string | null;
  build_status?: string | null;
}

export interface ExampleInfo { name: string; title: string; family: string; summary: string }

export interface ScenarioTrace {
  source: "verilator";
  revision_id: string;
  scenario_id: string;
  title: string;
  description: string;
  passed: boolean;
  requirement_ids: string[];
  clk_hz: number;
  end_cycle: number;
  duration_s: number;
  inputs: Array<{ cycle: number; port: string; value: number; t_s: number }>;
  /** signal -> [cycle, value, seconds] */
  outputs: Record<string, Array<[number, number, number]>>;
}

export interface VirtualEvent { t_s: number; port: "sw" | "btnC" | "btnU" | "btnL" | "btnR" | "btnD"; value: number }

export interface VirtualResult {
  source: "verilator";
  revision_id: string;
  revision_number: number;
  clk_hz: number;
  duration_s: number;
  end_cycle: number;
  note: string;
  /** signal -> [seconds, value] */
  outputs: Record<string, Array<[number, number]>>;
}

export interface DiagramNode {
  id: string;
  column: number;
  kind: "clock" | "input" | "rtl" | "functional" | "output";
  label: string;
  sub: string;
  source: "board" | "spec" | "rtl";
  component?: string;
  components?: string[];
}

export interface Diagram {
  revision_id: string;
  family: string;
  label: string;
  explanation: string;
  nodes: DiagramNode[];
  edges: Array<{ from: string; to: string; label?: string; clock?: boolean }>;
  requirements: Array<{ id: string; text: string }>;
}

export interface LlmStatus {
  provider: string;
  model: string;
  configured: boolean;
  mode: "model" | "offline-rules";
  key_present: boolean;
  key_source: string | null;
  env_file: string;
  env_file_exists: boolean;
  explanation: string;
}
