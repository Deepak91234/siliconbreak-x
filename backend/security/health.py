"""Dependency health for SiliconBreak X (credential-agnostic: never returns keys).

States: connected | configured | unverified | unavailable | verified | failed |
disconnected. Network checks use free endpoints only (model listings).
"""

from __future__ import annotations

import asyncio
import time

from backend.core.config import get_settings
from backend.security import formal as formal_mod
from backend.security import repair as rep
from backend.security.verify import verilator_version

_cache: dict | None = None
_cache_at = 0.0
_lock = asyncio.Lock()
CACHE_S = 30.0


async def _bitdeer(s) -> dict:
    out = {"name": "Bitdeer Model Studio", "role": "investigation (preferred)", "model": s.bitdeer_model,
           "base_url": s.bitdeer_base_url}
    if not s.bitdeer_api_key:
        return out | {"state": "unavailable", "detail": "BITDEER_API_KEY is not set"}
    from backend.agent.llm.base import LLMError
    from backend.agent.llm.bitdeer_provider import BitdeerProvider
    try:
        models = await asyncio.wait_for(BitdeerProvider(s.bitdeer_api_key, s.bitdeer_model,
                                                        base_url=s.bitdeer_base_url, timeout_s=15).list_models(), 20)
    except LLMError as e:
        if e.status_code == 404:
            return out | {"state": "configured", "detail": "key set; the models endpoint is not offered, so the model is unverified"}
        return out | {"state": "unavailable" if e.kind == "auth" else "unverified", "detail": e.message}
    except asyncio.TimeoutError:
        return out | {"state": "unverified", "detail": "model listing timed out"}
    found = s.bitdeer_model in models
    return out | {"state": "connected" if found else "configured",
                  "detail": f"{len(models)} models listed" + ("" if found else f"; {s.bitdeer_model} is not among them"),
                  "models": models[:40]}


async def _anthropic(s) -> dict:
    out = {"name": "Anthropic Claude", "role": "investigation / repair (alternate)", "model": s.llm_model}
    if not s.anthropic_api_key:
        return out | {"state": "unavailable", "detail": "ANTHROPIC_API_KEY is not set"}
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key, max_retries=0)
        page = await asyncio.wait_for(client.models.list(limit=50), 20)
        ids = [m.id for m in page.data]
        return out | {"state": "connected", "detail": f"{len(ids)} models listed"}
    except Exception as e:  # noqa: BLE001 - report, never raise
        msg = str(e).replace(s.anthropic_api_key, "***")[:300]
        return out | {"state": "unverified", "detail": f"{type(e).__name__}: {msg}"}


async def collect(force: bool = False) -> dict:
    global _cache, _cache_at
    async with _lock:
        if not force and _cache is not None and time.time() - _cache_at < CACHE_S:
            return _cache
        s = get_settings()
        from backend.api import environment

        bitdeer, claude, verilator, yosys, env = await asyncio.gather(
            _bitdeer(s), _anthropic(s), verilator_version(), formal_mod.yosys_version(), environment.collect())
        droid = rep.droid_status(s)
        viv = env.get("vivado") or {}
        hw = env.get("hardware") or {}
        hw_state = hw.get("state") or hw.get("status")
        out = {
            "checked_at": time.time(),
            "providers": {
                "bitdeer": bitdeer,
                "anthropic": claude,
                "factory": {"name": "Factory Droid", "role": "repair agent", "state": droid["state"],
                            "detail": droid.get("reason") or f"droid CLI {droid['cli']}, SDK {droid['sdk']}",
                            "sdk": droid["sdk"], "cli": droid["cli"]},
                "rules": {"name": "Offline static rules", "role": "fallback investigator (not AI)", "state": "connected",
                          "detail": "always available; deterministic pattern matching"},
            },
            "tools": {
                "verilator": {"state": "verified" if verilator else "unavailable", "detail": verilator or "not found"},
                "yosys": {"state": "verified" if yosys else "unavailable",
                          "detail": (yosys or "not found") + " (formal via built-in SAT; SymbiYosys not required)"},
                "vivado": {"state": "verified" if viv.get("ready") else "unverified",
                           "detail": f"{viv.get('active_container') or 'no container'} {viv.get('vivado_version') or ''}".strip()
                           + ("" if viv.get("ready") else ": " + "; ".join(viv.get("problems") or ["not ready"]))},
                "fpga": {"state": "connected" if hw_state == "connected" else "disconnected",
                         "detail": hw.get("message") or hw_state or "unknown"},
            },
            "routing": {"investigator": s.security_investigator, "repair_model": s.security_repair_model},
        }
        _cache, _cache_at = out, time.time()
        return out
