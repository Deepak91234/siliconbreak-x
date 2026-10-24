"""The trusted verifier. It alone decides pass/fail for a security case.

Given a case manifest and candidate versions of the case's editable files, it:

  1. copies the candidates into its own directory (the agent's workspace is
     never compiled in place, so nothing can change between check and use),
  2. rejects files outside the case's editable allowlist and runs the host-safety
     scan on every candidate,
  3. records sha256 hashes of the trusted testbench/formal files and the candidates,
  4. lints the design with Verilator (CircuitLoop's lint policy),
  5. builds the trusted testbench + candidates with `verilator --binary --timing`,
  6. runs it with a per-run nonce and a timeout, and parses only verdict lines
     that carry the nonce; a test the manifest lists but the run never reported
     is NOT_RUN, which makes the whole verdict an error.

Statuses: passed | failed | error | timeout | not_run. Model prose never enters
this module; its inputs are files and the manifest.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend.core.config import REPO_ROOT
from backend.core.process import LineCallback, ProcessResult, run_command
from backend.security.cases import CaseManifest, sha256_file
from backend.security.hostsafety import scan_source
from backend.verification.verilator import LINT_POLICY, VerilatorUnavailable, parse_messages, verilator_bin

VerifyStatus = Literal["passed", "failed", "error", "timeout", "not_run"]

_VERDICT_RE = re.compile(r"^SB_TEST (?P<nonce>\S+) (?P<id>[A-Z][A-Z0-9-]*) (?P<res>PASS|FAIL)(?: (?P<msg>.*))?$")
_SPAN_RE = re.compile(r"^SB_SPAN (?P<nonce>\S+) (?P<id>[A-Z][A-Z0-9-]*) (?P<t0>\d+) (?P<t1>\d+)$")
_DONE_RE = re.compile(r"^SB_DONE (?P<nonce>\S+)$")
_ERROR_RE = re.compile(r"^SB_ERROR (?P<nonce>\S+) (?P<msg>.*)$")


class CommandRecord(BaseModel):
    step: str
    command: str
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    timed_out: bool = False
    error: Optional[str] = None


class TestOutcome(BaseModel):
    id: str
    kind: str
    title: str = ""
    properties: list[str] = Field(default_factory=list)
    status: Literal["pass", "fail", "not_run"]
    message: str = ""
    span_ns: Optional[tuple[int, int]] = None


class VerificationReport(BaseModel):
    label: str
    case_id: str
    status: VerifyStatus
    security_status: Literal["passed", "failed", "not_run"] = "not_run"
    functional_status: Literal["passed", "failed", "not_run"] = "not_run"
    message: str = ""
    tests: list[TestOutcome] = Field(default_factory=list)
    failed_properties: list[str] = Field(default_factory=list)
    lint_ok: Optional[bool] = None
    lint_messages: list[dict] = Field(default_factory=list)
    safety_issues: list[dict] = Field(default_factory=list)
    scope_violations: list[str] = Field(default_factory=list)
    commands: list[CommandRecord] = Field(default_factory=list)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    trusted_hashes: dict[str, str] = Field(default_factory=dict)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    vcd: Optional[str] = None          # relative to the report's directory
    log: Optional[str] = None
    started_at: float = 0.0
    duration_s: float = 0.0

    def counts(self) -> dict:
        out = {"security": {"pass": 0, "fail": 0, "not_run": 0}, "functional": {"pass": 0, "fail": 0, "not_run": 0}}
        for t in self.tests:
            out.setdefault(t.kind, {"pass": 0, "fail": 0, "not_run": 0})[t.status] += 1
        return out


def display_command(argv: list[str]) -> str:
    """argv as a readable shell line with repository paths shortened (for the UI and evidence)."""
    root = str(REPO_ROOT) + "/"
    out = []
    for a in argv:
        a = a.replace(root, "")
        out.append(a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\\''") + "'")
    return " ".join(out)


def _record(step: str, proc: ProcessResult) -> CommandRecord:
    return CommandRecord(step=step, command=display_command(proc.argv), exit_code=proc.exit_code,
                         duration_s=round(proc.duration_s, 3), timed_out=proc.timed_out, error=proc.error)


async def verilator_version() -> str | None:
    try:
        proc = await run_command([verilator_bin(), "--version"], timeout_s=20)
    except VerilatorUnavailable:
        return None
    return proc.output[0].strip() if proc.ok and proc.output else None


async def verify_sources(
    case: CaseManifest,
    candidates: dict[str, Path],
    out_dir: Path,
    *,
    label: str,
    vcd: bool = True,
    on_line: LineCallback | None = None,
) -> VerificationReport:
    """Verify candidate files (editable path -> file on disk) against the case's trusted tests."""
    t0 = time.monotonic()
    rep = VerificationReport(label=label, case_id=case.case_id, status="not_run", started_at=time.time())
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "src").mkdir(parents=True)
    log_lines: list[str] = []

    async def log(line: str) -> None:
        log_lines.append(line)
        if on_line is not None:
            r = on_line(line)
            if hasattr(r, "__await__"):
                await r

    def finish(status: VerifyStatus, message: str) -> VerificationReport:
        rep.status = status
        rep.message = message
        rep.duration_s = round(time.monotonic() - t0, 3)
        (out_dir / "verify.log").write_text("\n".join(log_lines) + "\n")
        rep.log = "verify.log"
        (out_dir / "report.json").write_text(rep.model_dump_json(indent=2))
        return rep

    # ---- scope: only the case's editable files, all of them present
    scope = [rel for rel in candidates if rel not in case.editable]
    missing = [rel for rel in case.editable if rel not in candidates]
    if scope or missing:
        rep.scope_violations = [f"not editable: {r}" for r in scope] + [f"missing: {r}" for r in missing]
        return finish("error", "candidate does not match the case's editable file list")

    sources: list[Path] = []
    for rel in case.editable:
        src = Path(candidates[rel])
        if src.is_symlink() or not src.is_file():
            rep.scope_violations.append(f"{rel}: not a regular file")
            continue
        dst = out_dir / "src" / Path(rel).name
        shutil.copyfile(src, dst)
        sources.append(dst)
        rep.source_hashes[rel] = sha256_file(dst)
        try:
            text = dst.read_text()
        except UnicodeDecodeError:
            rep.safety_issues.append({"file": rel, "line": 1, "message": "file is not UTF-8 text"})
            continue
        rep.safety_issues += [i.as_dict() | {"file": rel} for i in scan_source(rel, text)]
    if rep.scope_violations:
        return finish("error", "candidate files are not regular files")
    rep.trusted_hashes = case.trusted_hashes()
    if rep.safety_issues:
        await log(f"host-safety scan rejected the candidate: {len(rep.safety_issues)} issue(s)")
        return finish("error", "host-safety scan rejected the candidate; it was not compiled")

    try:
        vbin = verilator_bin()
    except VerilatorUnavailable as e:
        return finish("not_run", str(e))
    ver = await verilator_version()
    if ver:
        rep.tool_versions["verilator"] = ver

    # ---- lint (design only)
    # warnings are reported but only CircuitLoop's fatal lint codes reject a candidate
    argv = [vbin, "--lint-only", "-Wall", "-Wno-fatal", "--top-module", case.top_module, str(LINT_POLICY),
            *map(str, sources)]
    await log("$ " + display_command(argv))
    proc = await run_command(argv, cwd=out_dir, timeout_s=120, on_line=log)
    rep.commands.append(_record("lint", proc))
    msgs = parse_messages(proc.output)
    rep.lint_messages = [m.as_dict() for m in msgs]
    rep.lint_ok = proc.ok and not any(m.fatal for m in msgs)
    if proc.error or proc.timed_out or not rep.lint_ok:
        return finish("error", "Verilator lint failed: " + ("; ".join(m.message for m in msgs if m.fatal)[:400]
                                                            or proc.error or "see log"))

    # ---- build the trusted testbench against the candidate
    tb = case.path(case.verification.testbench)
    obj = out_dir / "obj"
    argv = [vbin, "--binary", "--timing", "-j", "0", "-Wno-fatal", "-Wno-lint", "-Wno-style",
            "-CFLAGS", "-std=c++17", "-CFLAGS", "-Wno-unknown-warning-option",
            "--top-module", case.verification.tb_top, "-Mdir", str(obj), "-o", "Vtb"]
    if vcd:
        argv += ["--trace"]
    argv += [*map(str, sources), str(tb)]
    await log("$ " + display_command(argv))
    proc = await run_command(argv, cwd=out_dir, timeout_s=600, on_line=log)
    rep.commands.append(_record("build", proc))
    binary = obj / "Vtb"
    if not proc.ok or not binary.is_file():
        errs = [m.message for m in parse_messages(proc.output) if m.severity == "error"]
        return finish("error", "testbench build failed: " + ("; ".join(errs)[:400] or proc.error or "see log"))

    # ---- run
    nonce = secrets.token_hex(8)
    argv = [str(binary), f"+sb_nonce={nonce}"] + (["+vcd"] if vcd else [])
    await log("$ " + display_command(argv).replace(nonce, "<nonce>"))
    proc = await run_command(argv, cwd=out_dir, timeout_s=case.verification.timeout_s, on_line=log)
    rep.commands.append(_record("simulate", proc).model_copy(update={"command": display_command(argv).replace(nonce, "<nonce>")}))
    results: dict[str, tuple[str, str]] = {}
    spans: dict[str, tuple[int, int]] = {}
    done = False
    tb_errors: list[str] = []
    for line in proc.output:
        line = line.strip()
        if m := _VERDICT_RE.match(line):
            if m["nonce"] == nonce and m["id"] not in results:
                results[m["id"]] = (m["res"], (m["msg"] or "").strip())
        elif m := _SPAN_RE.match(line):
            if m["nonce"] == nonce:
                spans[m["id"]] = (int(m["t0"]), int(m["t1"]))
        elif m := _DONE_RE.match(line):
            done = done or m["nonce"] == nonce
        elif m := _ERROR_RE.match(line):
            if m["nonce"] == nonce:
                tb_errors.append(m["msg"])
    if vcd and (out_dir / "waves.vcd").is_file():
        rep.vcd = "waves.vcd"

    for t in case.verification.tests:
        res = results.get(t.id)
        rep.tests.append(TestOutcome(
            id=t.id, kind=t.kind, title=t.title, properties=t.properties,
            status="not_run" if res is None else ("pass" if res[0] == "PASS" else "fail"),
            message="" if res is None else res[1], span_ns=spans.get(t.id)))
    unknown = sorted(set(results) - {t.id for t in case.verification.tests})
    if unknown:
        await log(f"testbench reported tests the manifest does not list (ignored): {', '.join(unknown)}")

    def kind_status(kind: str) -> str:
        ts = [t for t in rep.tests if t.kind == kind]
        if not ts or any(t.status == "not_run" for t in ts):
            return "not_run"
        return "failed" if any(t.status == "fail" for t in ts) else "passed"

    rep.security_status = kind_status("security")  # type: ignore[assignment]
    rep.functional_status = kind_status("functional")  # type: ignore[assignment]
    rep.failed_properties = sorted({p for t in rep.tests if t.status == "fail" and t.kind == "security"
                                    for p in t.properties})
    if proc.timed_out:
        return finish("timeout", f"simulation exceeded {case.verification.timeout_s:.0f} s")
    if tb_errors:
        return finish("error", "testbench error: " + "; ".join(tb_errors))
    if not proc.ok or not done or any(t.status == "not_run" for t in rep.tests):
        n = sum(t.status == "not_run" for t in rep.tests)
        return finish("error", f"simulation did not complete (exit {proc.exit_code}, {n} test(s) not reported)")
    nfail = sum(t.status == "fail" for t in rep.tests)
    if nfail:
        return finish("failed", f"{nfail} of {len(rep.tests)} tests failed"
                      + (f"; violated: {', '.join(rep.failed_properties)}" if rep.failed_properties else ""))
    return finish("passed", f"all {len(rep.tests)} tests passed")


def load_report(path: Path) -> VerificationReport | None:
    try:
        return VerificationReport.model_validate(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return None
