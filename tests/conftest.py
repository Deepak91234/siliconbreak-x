import shutil

import pytest

PAID_KEYS = ("ANTHROPIC_API_KEY", "BITDEER_API_KEY", "FACTORY_API_KEY")


def pytest_addoption(parser):
    parser.addoption("--run-vivado", action="store_true", help="run real Vivado builds (slow)")
    parser.addoption("--run-hardware", action="store_true", help="run tests that touch a connected Basys 3")
    parser.addoption("--run-network", action="store_true", help="run tests that fetch from github.com")
    parser.addoption("--run-live-ai", action="store_true", help="allow tests marked live_ai to make billed AI calls")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "vivado" in item.keywords and not config.getoption("--run-vivado"):
            item.add_marker(pytest.mark.skip(reason="needs --run-vivado"))
        if "hardware" in item.keywords and not config.getoption("--run-hardware"):
            item.add_marker(pytest.mark.skip(reason="needs --run-hardware"))
        if "network" in item.keywords and not config.getoption("--run-network"):
            item.add_marker(pytest.mark.skip(reason="needs --run-network"))
        if "verilator" in item.keywords and not shutil.which("verilator"):
            item.add_marker(pytest.mark.skip(reason="verilator not installed"))


@pytest.fixture(autouse=True)
def _no_paid_ai(request, monkeypatch, tmp_path):
    """Credit protection for the test suite: the real API keys in .env are hidden, live AI calls
    stay off, and every test gets a throwaway data folder (never the real runs/ database), so no
    test can spend credits unless it is marked live_ai and --run-live-ai is given."""
    from backend.core import config
    from backend.storage import store

    if not ("live_ai" in request.keywords and request.config.getoption("--run-live-ai")):
        for k in PAID_KEYS:
            monkeypatch.setenv(k, "")
        monkeypatch.setenv("SILICONBREAK_LIVE_AI", "off")
    monkeypatch.setenv("CIRCUITLOOP_RUNS_DIR", str(tmp_path / "runs"))
    config.get_settings.cache_clear()
    store.get_db.cache_clear()
    yield
    store.get_db.cache_clear()
    config.get_settings.cache_clear()
