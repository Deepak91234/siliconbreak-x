"""AI credit protection: every paid model call and Droid session goes through here.

Rules (all enforced before a request is sent):
  * Live AI calls are OFF until the user turns them on in the dashboard (or sets
    SILICONBREAK_LIVE_AI=on). While off, providers are treated as unavailable and
    the pipeline uses its offline paths; nothing is billed.
  * Each call reserves its worst-case cost first: estimated input tokens
    (characters / 3, deliberately high) at the input price plus max_tokens at the
    output price (thinking tokens bill as output). The call is refused if that
    reservation would exceed the per-call cap, the provider's daily cap, or the
    provider's total cap (spent + in-flight reservations count).
  * After the call the reservation is settled with the provider's reported usage.
    If usage is unknown and the request may have been billed (timeouts, truncated
    or refused output), the worst case is kept on the books.
  * Factory Droid does not report dollars: sessions are capped per day, attempts
    per repair are capped, and the credits Droid reports are recorded.

Prices are USD per million tokens. Claude prices are Anthropic's published rates;
Bitdeer prices are conservative placeholders (overestimates) unless configured.
The ledger lives in the SiliconBreak X database (table ai_usage).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from backend.core.config import get_settings

ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
UNKNOWN_MODEL_PRICE = (10.00, 50.00)   # anything not listed is costed at the top tier
CHARS_PER_TOKEN = 3.0                   # conservative: real code/prose is ~3.5-4 chars per token
TYPICAL_SECURITY_CALL_CHARS = 14_000    # contract + RTL + schema, for UI estimates
TYPICAL_SECURITY_MAX_TOKENS = 16_000

_lock = threading.Lock()


class BudgetRefused(Exception):
    pass


@dataclass(frozen=True)
class Caps:
    total_usd: float
    daily_usd: float
    per_call_usd: float


def _db():
    from backend.storage import store
    return store.get_db()


# ------------------------------------------------------------------ live switch
def live_enabled() -> bool:
    row = _db().get("app_settings", "live_ai", key_col="key")
    if row is not None:
        return bool((row.get("value") or {}).get("enabled"))
    return get_settings().siliconbreak_live_ai


def set_live(enabled: bool) -> dict:
    db = _db()
    value = {"enabled": bool(enabled)}
    if db.get("app_settings", "live_ai", key_col="key") is None:
        db.insert("app_settings", {"key": "live_ai", "value": value, "updated_at": time.time()})
    else:
        db.update("app_settings", "live_ai", {"value": value, "updated_at": time.time()}, key_col="key")
    return summary()


# ------------------------------------------------------------------ prices / caps
def prices(provider: str, model: str | None) -> tuple[float, float]:
    s = get_settings()
    if provider == "anthropic":
        return ANTHROPIC_PRICES.get(model or "", UNKNOWN_MODEL_PRICE)
    if provider == "bitdeer":
        return (s.bitdeer_price_in, s.bitdeer_price_out)
    return UNKNOWN_MODEL_PRICE


def caps(provider: str) -> Caps:
    s = get_settings()
    total = {"anthropic": s.budget_anthropic_usd, "bitdeer": s.budget_bitdeer_usd}.get(provider, 0.0)
    return Caps(total_usd=total, daily_usd=min(s.budget_daily_usd, total), per_call_usd=s.budget_per_call_usd)


def estimate_call(provider: str, model: str | None, input_chars: int, max_tokens: int) -> float:
    pin, pout = prices(provider, model)
    tokens_in = math.ceil(input_chars / CHARS_PER_TOKEN) + 64
    return round((tokens_in * pin + max_tokens * pout) / 1_000_000, 6)


def cost_from_usage(provider: str, model: str | None, usage: dict) -> float | None:
    pin, pout = prices(provider, model)
    if provider == "anthropic":
        if "input_tokens" not in usage and "output_tokens" not in usage:
            return None
        tin = usage.get("input_tokens") or 0
        cw = usage.get("cache_creation_input_tokens") or 0
        cr = usage.get("cache_read_input_tokens") or 0
        tout = usage.get("output_tokens") or 0
        return round((tin * pin + cw * pin * 1.25 + cr * pin * 0.1 + tout * pout) / 1_000_000, 6)
    if provider == "bitdeer":
        if "prompt_tokens" not in usage and "completion_tokens" not in usage:
            return None
        return round(((usage.get("prompt_tokens") or 0) * pin + (usage.get("completion_tokens") or 0) * pout) / 1_000_000, 6)
    return None


# ------------------------------------------------------------------ ledger
def _midnight() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, t.tm_wday, t.tm_yday, t.tm_isdst))


def _committed(provider: str, since: float | None = None) -> float:
    """Settled cost plus in-flight reservations (worst case) for a provider."""
    q = ("SELECT COALESCE(SUM(CASE WHEN status = 'reserved' THEN max_cost_usd ELSE COALESCE(cost_usd, 0) END), 0) "
         "FROM ai_usage WHERE provider = ?")
    args: list = [provider]
    if since is not None:
        q += " AND ts >= ?"
        args.append(since)
    return float(_db().scalar(q, tuple(args)) or 0.0)


def reserve(provider: str, model: str | None, purpose: str, input_chars: int, max_tokens: int,
            run_id: str | None = None) -> str:
    from backend.storage.db import new_id

    if not live_enabled():
        raise BudgetRefused("live AI calls are off (turn them on in the dashboard's AI credits panel)")
    worst = estimate_call(provider, model, input_chars, max_tokens)
    c = caps(provider)
    with _lock:
        if worst > c.per_call_usd:
            raise BudgetRefused(f"this {provider} call could cost up to ${worst:.2f}, above the per-call cap ${c.per_call_usd:.2f}")
        total = _committed(provider)
        if total + worst > c.total_usd:
            raise BudgetRefused(f"{provider} budget would be exceeded: ${total:.2f} used or reserved of ${c.total_usd:.2f}")
        today = _committed(provider, _midnight())
        if today + worst > c.daily_usd:
            raise BudgetRefused(f"{provider} daily cap would be exceeded: ${today:.2f} of ${c.daily_usd:.2f} today")
        rid = new_id("ai")
        _db().insert("ai_usage", {"id": rid, "ts": time.time(), "provider": provider, "model": model, "purpose": purpose,
                                  "status": "reserved", "max_cost_usd": worst, "run_id": run_id, "detail": {}})
    return rid


# error kinds after which the request may still have been billed
_MAYBE_BILLED = {"network", "invalid_output", "refusal"}


def settle(rid: str, *, ok: bool, usage: dict | None = None, model: str | None = None,
           error_kind: str | None = None) -> float:
    db = _db()
    row = db.get("ai_usage", rid)
    if row is None:
        return 0.0
    cost = cost_from_usage(row["provider"], model or row["model"], usage or {}) if usage else None
    if cost is None:
        cost = row["max_cost_usd"] if (ok or error_kind in _MAYBE_BILLED) else 0.0
    u = usage or {}
    db.update("ai_usage", rid, {
        "status": "ok" if ok else "error", "cost_usd": cost, "model": model or row["model"],
        "input_tokens": u.get("input_tokens") or u.get("prompt_tokens"),
        "output_tokens": u.get("output_tokens") or u.get("completion_tokens"),
        "detail": {"error_kind": error_kind, "usage_reported": bool(usage)},
    })
    return float(cost)


def refuse_record(provider: str, model: str | None, purpose: str, reason: str) -> None:
    from backend.storage.db import new_id
    _db().insert("ai_usage", {"id": new_id("ai"), "ts": time.time(), "provider": provider, "model": model,
                              "purpose": purpose, "status": "refused", "max_cost_usd": 0.0, "cost_usd": 0.0,
                              "detail": {"reason": reason}})


# ------------------------------------------------------------------ factory droid
def reserve_droid_session(purpose: str, run_id: str | None = None) -> str:
    from backend.storage.db import new_id

    if not live_enabled():
        raise BudgetRefused("live AI calls are off (turn them on in the dashboard's AI credits panel)")
    cap = get_settings().droid_sessions_per_day
    with _lock:
        n = int(_db().scalar("SELECT COUNT(*) FROM ai_usage WHERE provider = 'factory' AND status != 'refused' AND ts >= ?",
                             (_midnight(),)) or 0)
        if n >= cap:
            raise BudgetRefused(f"Factory Droid daily session cap reached ({n}/{cap})")
        rid = new_id("ai")
        _db().insert("ai_usage", {"id": rid, "ts": time.time(), "provider": "factory", "model": get_settings().droid_model,
                                  "purpose": purpose, "status": "reserved", "max_cost_usd": 0.0, "run_id": run_id,
                                  "detail": {}})
    return rid


def settle_droid(rid: str, *, ok: bool, usage: dict | None, model: str | None = None) -> None:
    u = usage or {}
    _db().update("ai_usage", rid, {"status": "ok" if ok else "error", "cost_usd": 0.0, "model": model,
                                   "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                                   "detail": {"factory_credits": u.get("factory_credits"), "usage": u}})


# ------------------------------------------------------------------ summary
def summary(recent: int = 12) -> dict:
    s = get_settings()
    out: dict = {"live_enabled": live_enabled(), "providers": {}, "recent": []}
    for p in ("anthropic", "bitdeer"):
        c = caps(p)
        model = s.llm_model if p == "anthropic" else s.bitdeer_model
        pin, pout = prices(p, model)
        out["providers"][p] = {
            "configured": bool(s.anthropic_api_key if p == "anthropic" else s.bitdeer_api_key),
            "model": model, "price_in_per_mtok": pin, "price_out_per_mtok": pout,
            "prices_are_estimates": p == "bitdeer",
            "cap_total_usd": c.total_usd, "cap_daily_usd": c.daily_usd, "cap_per_call_usd": c.per_call_usd,
            "used_usd": round(_committed(p), 4), "used_today_usd": round(_committed(p, _midnight()), 4),
            "calls": int(_db().scalar("SELECT COUNT(*) FROM ai_usage WHERE provider = ? AND status IN ('ok','error')", (p,)) or 0),
            "worst_case_per_call_usd": estimate_call(p, model, TYPICAL_SECURITY_CALL_CHARS, TYPICAL_SECURITY_MAX_TOKENS),
        }
    n_today = int(_db().scalar("SELECT COUNT(*) FROM ai_usage WHERE provider = 'factory' AND status != 'refused' AND ts >= ?",
                               (_midnight(),)) or 0)
    credits = _db().query("SELECT detail_json FROM ai_usage WHERE provider = 'factory' AND status IN ('ok','error')")
    reported = [r["detail"].get("factory_credits") for r in credits if (r.get("detail") or {}).get("factory_credits") is not None]
    out["providers"]["factory"] = {
        "configured": bool(s.factory_api_key), "model": s.droid_model or "droid default",
        "sessions_today": n_today, "cap_sessions_per_day": s.droid_sessions_per_day,
        "max_attempts_per_repair": s.droid_max_attempts,
        "credits_reported_total": round(sum(float(x) for x in reported), 4) if reported else None,
        "sessions_total": int(_db().scalar("SELECT COUNT(*) FROM ai_usage WHERE provider = 'factory' AND status IN ('ok','error')") or 0),
    }
    rows = _db().query("SELECT * FROM ai_usage ORDER BY ts DESC LIMIT ?", (recent,))
    out["recent"] = [{k: r.get(k) for k in ("ts", "provider", "model", "purpose", "status", "cost_usd", "max_cost_usd",
                                            "input_tokens", "output_tokens")} | {"detail": r.get("detail")} for r in rows]
    return out
