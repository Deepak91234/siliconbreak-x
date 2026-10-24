import shutil

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-vivado", action="store_true", help="run real Vivado builds (slow)")
    parser.addoption("--run-hardware", action="store_true", help="run tests that touch a connected Basys 3")
    parser.addoption("--run-network", action="store_true", help="run tests that fetch from github.com")


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
