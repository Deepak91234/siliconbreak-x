"""Restricted subprocess execution.

Every external tool CircuitLoop runs (verilator, docker, openFPGALoader, ioreg,
pgrep) goes through `run_command`, which:
  * takes an argv list (never a shell string; no shell=True anywhere),
  * streams output lines to a callback as they arrive,
  * enforces a timeout and supports cooperative cancellation,
  * returns exit status and the captured output.

Callers build argv from validated values only. LLM output is never placed in
an argv; generated Verilog only reaches tools as files inside the run workspace.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

LineCallback = Callable[[str], "Awaitable[None] | None"]


@dataclass
class ProcessResult:
    argv: list[str]
    exit_code: int | None
    output: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None  # e.g. executable not found

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled and self.error is None

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class PathOutsideWorkspace(ValueError):
    pass


def ensure_within(path: Path, root: Path) -> Path:
    """Resolve `path` and require it to be inside `root` (symlinks resolved)."""
    rp = Path(path).resolve()
    rr = Path(root).resolve()
    if rp != rr and rr not in rp.parents:
        raise PathOutsideWorkspace(f"{rp} is outside workspace {rr}")
    return rp


async def _emit(cb: LineCallback | None, line: str) -> None:
    if cb is None:
        return
    r = cb(line)
    if inspect.isawaitable(r):
        await r


async def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_s: float = 120.0,
    on_line: LineCallback | None = None,
    env: dict[str, str] | None = None,
    cancel: asyncio.Event | None = None,
    max_lines: int = 200_000,
    stdin_data: bytes | None = None,
) -> ProcessResult:
    argv = [str(a) for a in argv]
    start = time.monotonic()
    result = ProcessResult(argv=argv, exit_code=None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})},
            start_new_session=True,
        )
    except FileNotFoundError as e:
        result.error = f"executable not found: {argv[0]} ({e})"
        result.duration_s = time.monotonic() - start
        return result
    except PermissionError as e:
        result.error = f"permission denied: {argv[0]} ({e})"
        result.duration_s = time.monotonic() - start
        return result

    if stdin_data is not None and proc.stdin is not None:
        proc.stdin.write(stdin_data)
        await proc.stdin.drain()
        proc.stdin.close()

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if len(result.output) < max_lines:
                result.output.append(line)
            await _emit(on_line, line)

    pump_task = asyncio.create_task(pump())
    wait_task = asyncio.create_task(proc.wait())
    cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
    waiters = {wait_task} | ({cancel_task} if cancel_task else set())
    done, _ = await asyncio.wait(waiters, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)

    if wait_task not in done:
        if cancel_task is not None and cancel_task in done:
            result.cancelled = True
        else:
            result.timed_out = True
        _terminate(proc)
        try:
            await asyncio.wait_for(wait_task, timeout=10)
        except asyncio.TimeoutError:
            _kill(proc)
            await wait_task
    if cancel_task is not None:
        cancel_task.cancel()
    try:
        await asyncio.wait_for(pump_task, timeout=10)
    except asyncio.TimeoutError:
        pump_task.cancel()
    result.exit_code = proc.returncode
    result.duration_s = time.monotonic() - start
    return result


def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
