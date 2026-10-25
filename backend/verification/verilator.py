"""Verilator tool adapter: lint, harness build, simulation run, trace parsing.

All commands are fixed argv templates. Inputs are file paths that must lie inside
the caller-supplied workspace and integer -G parameter overrides whose names are
validated identifiers.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.config import get_settings
from backend.core.process import LineCallback, ProcessResult, ensure_within, run_command

HARNESS_CPP = Path(__file__).resolve().parent / "harness" / "tb_main.cpp"
LINT_POLICY = Path(__file__).resolve().parent / "lint_policy.vlt"
TOP = "basys3_top"

_PARAM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MSG_RE = re.compile(
    r"^%(?P<sev>Warning|Error)(?:-(?P<code>[A-Z0-9_]+))?:\s*"
    r"(?:(?P<file>[^:\s]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*)?(?P<msg>.*)$"
)

# Lint codes that SiliconBreak X treats as design errors even though Verilator
# reports them as warnings. Anything else is reported but not fatal.
FATAL_LINT_CODES = {
    "WIDTHTRUNC", "WIDTHEXPAND", "WIDTH", "LATCH", "COMBDLY", "MULTIDRIVEN",
    "UNOPTFLAT", "BLKSEQ", "CASEINCOMPLETE", "CASEX", "IMPLICIT", "SELRANGE",
    "PINMISSING", "PINCONNECTEMPTY", "UNDRIVEN", "MULTITOP", "INITIALDLY",
    "NOLATCH", "SYNCASYNCNET", "ALWCOMBORDER", "BLKANDNBLK", "CMPCONST",
    "UNSIGNED", "ASCRANGE", "ENUMVALUE", "LITENDIAN",
}


class VerilatorUnavailable(RuntimeError):
    pass


@dataclass
class LintMessage:
    severity: str  # "warning" | "error"
    code: str | None
    file: str | None
    line: int | None
    column: int | None
    message: str
    fatal: bool
    context: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class LintResult:
    ok: bool
    messages: list[LintMessage]
    process: ProcessResult

    @property
    def errors(self) -> list[LintMessage]:
        return [m for m in self.messages if m.fatal]

    @property
    def warnings(self) -> list[LintMessage]:
        return [m for m in self.messages if not m.fatal]


@dataclass
class BuildResult:
    ok: bool
    binary: Path | None
    process: ProcessResult
    messages: list[LintMessage]


@dataclass
class SimRun:
    ok: bool
    trace_path: Path
    vcd_path: Path | None
    process: ProcessResult


@dataclass
class Trace:
    end_cycle: int
    changes: dict[str, list[tuple[int, int]]]  # signal -> [(cycle, value)] (first entry at cycle 0)

    def value_at(self, signal: str, cycle: int) -> int:
        val = 0
        for c, v in self.changes.get(signal, []):
            if c > cycle:
                break
            val = v
        return val


def verilator_bin() -> str:
    b = get_settings().verilator_bin
    if not b:
        raise VerilatorUnavailable("verilator not found (brew install verilator)")
    return b


def _param_args(params: dict[str, int]) -> list[str]:
    args = []
    for name, value in params.items():
        if not _PARAM_RE.match(name):
            raise ValueError(f"invalid parameter name {name!r}")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"parameter {name} must be an int")
        args.append(f"-G{name}={value}")
    return args


def parse_messages(lines: list[str]) -> list[LintMessage]:
    msgs: list[LintMessage] = []
    for line in lines:
        m = _MSG_RE.match(line)
        if m:
            sev = m["sev"].lower()
            code = m["code"]
            msgs.append(
                LintMessage(
                    severity=sev,
                    code=code,
                    file=m["file"],
                    line=int(m["line"]) if m["line"] else None,
                    column=int(m["col"]) if m["col"] else None,
                    message=m["msg"].strip(),
                    fatal=(sev == "error") or (code in FATAL_LINT_CODES),
                )
            )
        elif msgs and line.startswith(" "):
            if len(msgs[-1].context) < 12:
                msgs[-1].context.append(line)
    # "%Error: Exiting due to N warning(s)" is a summary line, not a finding.
    return [m for m in msgs if not m.message.startswith("Exiting due to")]


def _check_sources(sources: list[Path], workspace: Path) -> list[Path]:
    """Sources must be in the workspace or be SiliconBreak X's own library files."""
    from backend.rtl.wrapper import LIBRARY_DIR

    checked = []
    for s in sources:
        p = Path(s).resolve()
        if LIBRARY_DIR.resolve() in p.parents:
            checked.append(p)
        else:
            checked.append(ensure_within(p, workspace))
    return checked


async def lint(
    sources: list[Path],
    workspace: Path,
    params: dict[str, int] | None = None,
    top: str = TOP,
    on_line: LineCallback | None = None,
    timeout_s: float = 120,
) -> LintResult:
    srcs = _check_sources(sources, workspace)
    argv = [verilator_bin(), "--lint-only", "-Wall", "--top-module", top, str(LINT_POLICY)]
    argv += _param_args(params or {})
    argv += [str(s) for s in srcs]
    proc = await run_command(argv, cwd=workspace, timeout_s=timeout_s, on_line=on_line)
    msgs = parse_messages(proc.output)
    ok = proc.ok and not any(m.fatal for m in msgs)
    if proc.error or proc.timed_out:
        ok = False
    return LintResult(ok=ok, messages=msgs, process=proc)


async def build_harness(
    sources: list[Path],
    workspace: Path,
    params: dict[str, int],
    obj_dir: Path,
    trace: bool = True,
    on_line: LineCallback | None = None,
    timeout_s: float = 600,
) -> BuildResult:
    srcs = _check_sources(sources, workspace)
    obj_dir = ensure_within(obj_dir, workspace)
    if obj_dir.exists():
        shutil.rmtree(obj_dir)
    obj_dir.mkdir(parents=True)
    argv = [
        verilator_bin(), "--cc", "--exe", "--build", "-j", "0", "-O2",
        "-Wno-fatal", "-Wno-lint", "-Wno-style",
        # Homebrew's verilator bottle does not pass a C++ standard to Apple clang 15
        "-CFLAGS", "-std=c++17",
        # the bottle also passes -Wno-<flag> options Apple clang 15 does not know; silence that noise
        "-CFLAGS", "-Wno-unknown-warning-option",
        "--x-assign", "unique", "--x-initial", "unique",
        "--top-module", TOP, "-Mdir", str(obj_dir), "-o", "Vsim",
        str(LINT_POLICY),
    ]
    if trace:
        argv += ["--trace", "--trace-depth", "4"]
    argv += _param_args(params)
    argv += [str(s) for s in srcs] + [str(HARNESS_CPP)]
    proc = await run_command(argv, cwd=workspace, timeout_s=timeout_s, on_line=on_line)
    binary = obj_dir / "Vsim"
    ok = proc.ok and binary.is_file()
    return BuildResult(ok=ok, binary=binary if ok else None, process=proc, messages=parse_messages(proc.output))


async def run_harness(
    binary: Path,
    workspace: Path,
    stimulus: Path,
    trace_out: Path,
    vcd: tuple[Path, int, int] | None = None,
    seed: int = 1,
    timeout_s: float = 300,
    on_line: LineCallback | None = None,
) -> SimRun:
    binary = ensure_within(binary, workspace)
    stimulus = ensure_within(stimulus, workspace)
    trace_out = ensure_within(trace_out, workspace)
    argv = [str(binary), str(stimulus), str(trace_out)]
    vcd_path = None
    if vcd is not None:
        vcd_path = ensure_within(vcd[0], workspace)
        argv += [str(vcd_path), str(int(vcd[1])), str(int(vcd[2]))]
    # Randomise every uninitialised bit (x-initial unique) so designs cannot rely on X == 0.
    argv += ["+verilator+rand+reset+2", f"+verilator+seed+{int(seed)}"]
    proc = await run_command(argv, cwd=workspace, timeout_s=timeout_s, on_line=on_line)
    ok = proc.ok and trace_out.is_file()
    return SimRun(ok=ok, trace_path=trace_out, vcd_path=vcd_path if ok else None, process=proc)


def parse_trace(path: Path) -> Trace:
    changes: dict[str, list[tuple[int, int]]] = {}
    end = 0
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "end":
            end = int(parts[1])
            continue
        cycle, sig, val = int(parts[0]), parts[1], int(parts[2], 16)
        changes.setdefault(sig, []).append((cycle, val))
    return Trace(end_cycle=end, changes=changes)


async def version() -> str | None:
    try:
        proc = await run_command([verilator_bin(), "--version"], timeout_s=20)
    except VerilatorUnavailable:
        return None
    return proc.output[0].strip() if proc.ok and proc.output else None
