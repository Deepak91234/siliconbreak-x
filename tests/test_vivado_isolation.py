"""Isolated Vivado layout: SiliconBreak X only uses a container whose Vivado install mount is
read-only, and its headless start creates exactly that container (no network, capped memory)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.config import get_settings
from backend.core.process import ProcessResult
from backend.vivado import container as ctr


def _settings(tmp_path: Path):
    s = get_settings()
    install = tmp_path / "install"
    (install / "Vivado" / "2023.2").mkdir(parents=True)
    root = tmp_path / "sbx_root"
    root.mkdir()
    return s.__class__(**{**s.__dict__, "vivado_host_root": root, "vivado_install_dir": install,
                          "vivado_container": "siliconbreak_vivado", "vivado_headless_container": "siliconbreak_vivado",
                          "vivado_container_home": "/home/user", "vivado_workspace_subdir": "ws",
                          "docker_bin": "/usr/bin/true"}), root, install


@pytest.fixture
def fake_docker(monkeypatch):
    calls: list[list[str]] = []

    async def _docker(args, timeout_s=30, settings=None):
        calls.append(list(args))
        return ProcessResult(argv=["docker", *args], exit_code=0, output=["29.8.1"])

    async def _probe(env, s):
        return None
    monkeypatch.setattr(ctr, "_docker", _docker)
    monkeypatch.setattr(ctr, "_probe_inside", _probe)
    return calls


@pytest.mark.parametrize("rw,source_ok,expect_active", [(False, True, True), (True, True, False), (False, False, False)])
async def test_detect_requires_read_only_install(tmp_path, monkeypatch, fake_docker, rw, source_ok, expect_active):
    s, root, install = _settings(tmp_path)

    async def _inspect(name, settings):
        return ctr.ContainerInfo(name=name, exists=True, running=True, image="x64-linux", home_mount_source=str(root),
                                 install_mount_source=str(install if source_ok else tmp_path / "university_home"),
                                 install_mount_rw=rw)
    monkeypatch.setattr(ctr, "_inspect", _inspect)
    env = await ctr.detect(s)
    assert (env.active_container == "siliconbreak_vivado") is expect_active
    if not expect_active:
        assert any("read-only" in p for p in env.problems)


async def test_detect_refuses_university_home_mount(tmp_path, monkeypatch, fake_docker):
    s, root, install = _settings(tmp_path)

    async def _inspect(name, settings):
        return ctr.ContainerInfo(name=name, exists=True, running=True, image="x64-linux",
                                 home_mount_source=str(tmp_path / "cse100_home"), install_mount_source=str(install),
                                 install_mount_rw=False)
    monkeypatch.setattr(ctr, "_inspect", _inspect)
    env = await ctr.detect(s)
    assert env.active_container is None


async def test_headless_start_builds_the_isolated_container(tmp_path, monkeypatch):
    s, root, install = _settings(tmp_path)
    seen: list[list[str]] = []

    async def detect(settings=None):
        env = ctr.VivadoEnvironment(docker_cli="docker")
        env.docker_daemon, env.image_present, env.host_root_exists = True, True, True
        return env

    async def run_command(argv, timeout_s=120, **k):
        seen.append(list(argv))
        return ProcessResult(argv=list(argv), exit_code=0, output=["cid"])
    monkeypatch.setattr(ctr, "detect", detect)
    monkeypatch.setattr(ctr, "run_command", run_command)
    await ctr.start_headless(s)
    argv = seen[0]
    joined = " ".join(argv)
    assert "--network none" in joined and "--oom-score-adj 1000" in joined and "--memory 2560m" in joined
    assert f"type=bind,source={install},target=/home/user/Xilinx,readonly" in argv
    assert f"type=bind,source={root},target=/home/user" in argv
    assert "-p" not in argv and "--privileged" not in argv
    assert (root / "ws").is_dir() and (root / "Xilinx").is_dir()
