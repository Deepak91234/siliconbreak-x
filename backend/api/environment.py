"""Aggregated environment status (Docker/Vivado, Verilator, openFPGALoader, LLM, hardware)."""

from __future__ import annotations

import asyncio
import time

from backend.core.config import get_settings
from backend.core.events import bus
from backend.core.process import run_command

_cache: dict | None = None
_cache_at = 0.0
_lock = asyncio.Lock()
CACHE_S = 10.0


async def _tool_version(path: str | None, args: list[str]) -> dict:
    if not path:
        return {"available": False, "path": None, "version": None}
    r = await run_command([path, *args], timeout_s=20)
    first = next((ln.strip() for ln in r.output if ln.strip()), None)
    return {"available": r.ok or bool(first), "path": path, "version": first}


async def collect(force: bool = False) -> dict:
    global _cache, _cache_at
    async with _lock:
        if not force and _cache is not None and time.time() - _cache_at < CACHE_S:
            return _cache
        from backend.programmer.manager import hardware
        from backend.vivado import build as vb
        from backend.vivado import container as ctr

        s = get_settings()
        viv, verilator, ofl, hw = await asyncio.gather(
            ctr.detect(s),
            _tool_version(s.verilator_bin, ["--version"]),
            _tool_version(s.openfpgaloader_bin, ["--Version"]),
            hardware.status(deep=False),
        )
        vd = viv.as_dict()
        vd["build_running"] = vb.is_busy()
        env = {
            "checked_at": time.time(),
            "vivado": vd,
            "verilator": verilator,
            "openfpgaloader": ofl,
            "llm": {"provider": s.llm_provider, "model": s.llm_model, "configured": s.llm_configured},
            "hardware": hw.model_dump() if hasattr(hw, "model_dump") else hw,
            "config": s.public_dict(),
        }
        changed = _cache is None or _strip(_cache) != _strip(env)
        _cache, _cache_at = env, time.time()
    if changed:
        await bus.emit("environment.updated", environment=env)
    return env


def _strip(env: dict) -> dict:
    """Compare environments ignoring timestamps."""
    out = {k: v for k, v in env.items() if k != "checked_at"}
    out["vivado"] = {k: v for k, v in env["vivado"].items() if k != "checked_at"}
    hw = dict(env.get("hardware") or {})
    hw.pop("checked_at", None)
    out["hardware"] = hw
    return out
