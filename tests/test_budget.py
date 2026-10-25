"""AI credit protection: live switch, per-call / daily / total caps, settlement from reported
usage, the provider wrapper, Droid session caps, and bench cost confirmation. No real calls."""

from __future__ import annotations

import importlib
import json

import httpx
import pytest

from backend.agent.llm.base import LLMError, LLMResult


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICONBREAK_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SILICONBREAK_BUDGET_ANTHROPIC_USD", "1.0")
    monkeypatch.setenv("SILICONBREAK_BUDGET_DAILY_USD", "0.8")
    monkeypatch.setenv("SILICONBREAK_BUDGET_PER_CALL_USD", "0.5")
    monkeypatch.setenv("SILICONBREAK_DROID_SESSIONS_PER_DAY", "2")
    from backend.core import budget, config
    from backend.storage import store

    config.get_settings.cache_clear()
    store.get_db.cache_clear()
    yield budget
    store.get_db.cache_clear()
    config.get_settings.cache_clear()


def test_live_ai_is_off_by_default_and_refuses(ledger):
    assert ledger.live_enabled() is False
    with pytest.raises(ledger.BudgetRefused, match="live AI calls are off"):
        ledger.reserve("anthropic", "claude-opus-5-5", "t", 1000, 1000)
    with pytest.raises(ledger.BudgetRefused):
        ledger.reserve_droid_session("t")
    assert ledger.set_live(True)["live_enabled"] is True and ledger.live_enabled()


def test_worst_case_reservation_and_caps(ledger):
    ledger.set_live(True)
    # Opus 5.5: 16k output tokens alone = $0.32; plus input
    worst = ledger.estimate_call("anthropic", "claude-opus-5-5", 9000, 16000)
    assert 0.32 < worst < 0.35
    with pytest.raises(ledger.BudgetRefused, match="per-call cap"):
        ledger.reserve("anthropic", "claude-opus-5-5", "t", 9000, 32000)      # $0.64 > $0.50
    r1 = ledger.reserve("anthropic", "claude-opus-5-5", "t", 9000, 16000)
    r2 = ledger.reserve("anthropic", "claude-opus-5-5", "t", 9000, 16000)    # in-flight ~$0.67 total
    with pytest.raises(ledger.BudgetRefused, match="daily cap"):
        ledger.reserve("anthropic", "claude-opus-5-5", "t", 9000, 16000)
    # settle with real usage: 3k in, 2k out = 0.012 + 0.04 = $0.052
    cost = ledger.settle(r1, ok=True, usage={"input_tokens": 3000, "output_tokens": 2000}, model="claude-opus-5-5")
    assert abs(cost - 0.052) < 1e-6
    # an auth failure is not billed; a timeout may have been, so it keeps the worst case
    assert ledger.settle(r2, ok=False, error_kind="auth") == 0.0
    r3 = ledger.reserve("anthropic", "claude-opus-5-5", "t", 9000, 16000)
    assert ledger.settle(r3, ok=False, error_kind="network") == pytest.approx(worst)
    s = ledger.summary()["providers"]["anthropic"]
    assert s["used_usd"] == pytest.approx(0.052 + worst, abs=1e-4) and s["calls"] == 3


def test_unknown_models_are_costed_at_the_top_tier(ledger):
    assert ledger.prices("anthropic", "some-future-model") == ledger.UNKNOWN_MODEL_PRICE
    assert ledger.prices("anthropic", "claude-haiku-5-5") == (0.10, 0.50)


def test_droid_session_cap(ledger):
    ledger.set_live(True)
    a = ledger.reserve_droid_session("t")
    ledger.settle_droid(a, ok=True, usage={"factory_credits": 1.5, "input_tokens": 10})
    ledger.reserve_droid_session("t")
    with pytest.raises(ledger.BudgetRefused, match="session cap"):
        ledger.reserve_droid_session("t")
    f = ledger.summary()["providers"]["factory"]
    assert f["sessions_today"] == 2 and f["credits_reported_total"] == 1.5


class _Fake:
    name = "anthropic"
    model = "claude-opus-5-5"

    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    async def structured(self, *, purpose, system, messages, output_model, max_tokens=16000):
        self.calls += 1
        if self.error:
            raise self.error
        v = output_model.model_validate({"summary": "ok", "areas_reviewed": [], "findings": []})
        return LLMResult[output_model](value=v, text="{}", model=self.model, provider=self.name,
                                       usage={"input_tokens": 1000, "output_tokens": 500})


async def test_budgeted_provider_reserves_settles_and_refuses(ledger):
    from backend.agent.llm.budgeted import BudgetedProvider
    from backend.security.investigate import InvestigationOut

    inner = _Fake()
    p = BudgetedProvider(inner, "anthropic")
    with pytest.raises(LLMError, match="credit guard"):           # live off: nothing is sent
        await p.structured(purpose="t", system="s", messages=[{"role": "user", "content": "u"}],
                           output_model=InvestigationOut, max_tokens=4000)
    assert inner.calls == 0
    ledger.set_live(True)
    await p.structured(purpose="t", system="s", messages=[{"role": "user", "content": "u"}],
                       output_model=InvestigationOut, max_tokens=4000)
    assert inner.calls == 1
    recent = ledger.summary()["recent"]
    assert recent[0]["status"] == "ok" and recent[0]["cost_usd"] == pytest.approx((1000 * 4 + 500 * 20) / 1e6)
    assert any(r["status"] == "refused" for r in recent)
    bad = BudgetedProvider(_Fake(error=LLMError("invalid_output", "truncated")), "anthropic")
    with pytest.raises(LLMError):
        await bad.structured(purpose="t", system="s", messages=[{"role": "user", "content": "u"}],
                             output_model=InvestigationOut, max_tokens=4000)
    assert ledger.summary()["recent"][0]["cost_usd"] > 0          # truncated output may be billed: worst case kept


def test_providers_stay_offline_while_live_is_off(ledger, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("BITDEER_API_KEY", "bd-test-not-real")
    from backend.agent.llm import get_provider
    from backend.core import config
    from backend.security import investigate as inv

    config.get_settings.cache_clear()
    assert get_provider() is None
    name, prov, note = inv.pick_provider("auto")
    assert name == "rules" and prov is None and "live AI calls are off" in note
    ledger.set_live(True)
    name, prov, _ = inv.pick_provider("auto")
    assert name == "bitdeer" and type(prov).__name__ == "BudgetedProvider"
    assert type(get_provider()).__name__ == "BudgetedProvider"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICONBREAK_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("BITDEER_API_KEY", "bd-test-not-real")
    from backend.core import config
    from backend.storage import store

    config.get_settings.cache_clear()
    store.get_db.cache_clear()
    import backend.api.app as appmod

    importlib.reload(appmod)
    async with appmod.app.router.lifespan_context(appmod.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app), base_url="http://127.0.0.1") as c:
            yield c
    store.get_db.cache_clear()
    config.get_settings.cache_clear()


async def test_bench_paid_arms_need_confirmation_and_defaults_are_free(client):
    c = client
    est = (await c.post("/api/bench/estimate", json={"arms": ["harness", "detect:bitdeer", "repair:assisted:auto"]})).json()
    assert est["paid"] and est["usd"]["bitdeer"] > 0 and est["calls"]["bitdeer"] == 3 * (1 + 3)
    r = await c.post("/api/bench/runs", json={"arms": ["detect:bitdeer"]})
    assert r.status_code == 409 and r.json()["detail"]["estimate"]["usd_total"] > 0
    r = await c.post("/api/bench/runs", json={"arms": ["detect:bitdeer"], "confirm_max_usd": 0.0001})
    assert r.status_code == 409
    free = (await c.post("/api/bench/estimate", json={})).json()
    assert free["arms"] == ["harness", "detect:rules"] and not free["paid"] and free["usd_total"] == 0


async def test_ai_budget_endpoints_and_live_toggle(client):
    c = client
    b = (await c.get("/api/ai/budget")).json()
    assert b["live_enabled"] is False and "bd-test" not in json.dumps(b)
    assert b["providers"]["bitdeer"]["prices_are_estimates"] is True
    assert (await c.post("/api/ai/live", json={"enabled": True})).json()["live_enabled"] is True
    assert (await c.post("/api/ai/live", json={"enabled": True}, headers={"origin": "https://evil.example"})).status_code == 403
    assert (await c.get("/api/llm")).json()["live_ai"] is True
