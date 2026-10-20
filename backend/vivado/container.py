"""Detection and lifecycle of the existing Vivado Docker environment.

Facts established by inspecting ~/cse100/cse100-vivado-on-silicon-mac-main/scripts
(start_container.sh, linux_start.sh, header.sh, Dockerfile):

  * Image `x64-linux` (ubuntu 22.04, linux/amd64 under Rosetta), user `user`.
  * start_container.sh runs it as `vivado_container` with
    --init --rm --mount type=bind,source=<repo>,target=/home/user -p 127.0.0.1:5901:5901
    and executes linux_start.sh (TigerVNC + LXDE; the desktop autostart launches
    hw_server pointed at xilinx-xvc:host.docker.internal:2542 and the Vivado GUI).
  * The launcher also opens Screen Sharing and keeps restarting the macOS `xvcd`
    USB bridge while the container is up; Ctrl+C kills the container and xvcd.
  * Vivado lives at /home/user/Xilinx/Vivado/<version>/ (2023.2 here) and needs the
    LD_PRELOAD that the image sets in its environment.

Policy implemented here:
  * If `vivado_container` is running it is reused as-is (docker exec only).
    CircuitLoop never restarts, stops or reconfigures it.
  * If no suitable container is running, CircuitLoop can -- only after explicit
    user approval -- start a separate *headless* container
    (`circuitloop_vivado`) from the same image with the same bind mount and no
    VNC/XVC. It is labelled `circuitloop.managed=true`; only containers with
    that label are ever stopped by CircuitLoop.
  * The launcher script itself is never executed (it would open Screen Sharing,
    start xvcd and take over the container lifecycle).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.core.config import Settings, get_settings
from backend.core.process import run_command

MANAGED_LABEL = "circuitloop.managed"
_VERSION_RE = re.compile(r"^\d{4}\.\d$")


@dataclass
class ContainerInfo:
    name: str
    exists: bool = False
    running: bool = False
    status: str | None = None
    image: str | None = None
    managed_by_circuitloop: bool = False
    home_mount_source: str | None = None
    started_at: str | None = None


@dataclass
class VivadoEnvironment:
    checked_at: float = field(default_factory=time.time)
    docker_cli: str | None = None
    docker_daemon: bool = False
    docker_server_version: str | None = None
    docker_error: str | None = None
    image_present: bool = False
    containers: list[ContainerInfo] = field(default_factory=list)
    active_container: str | None = None
    vivado_versions: list[str] = field(default_factory=list)
    vivado_version: str | None = None
    vivado_bin: str | None = None
    settings_script: str | None = None
    vivado_executable: bool = False
    gui_session_running: bool = False
    hw_server_running: bool = False
    batch_builds_running: int = 0
    workspace_host: str | None = None
    workspace_container: str | None = None
    workspace_shared: bool = False
    host_root_exists: bool = False
    can_start_headless: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.active_container and self.vivado_executable and self.workspace_shared)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ready"] = self.ready
        return d


async def _docker(args: list[str], timeout_s: float = 20, settings: Settings | None = None):
    s = settings or get_settings()
    if not s.docker_bin:
        raise FileNotFoundError("docker CLI not found")
    return await run_command([s.docker_bin, *args], timeout_s=timeout_s)


async def _inspect(name: str, s: Settings) -> ContainerInfo:
    info = ContainerInfo(name=name)
    r = await _docker(["inspect", "--type", "container", name], settings=s)
    if not r.ok:
        return info
    try:
        data = json.loads(r.text)[0]
    except (ValueError, IndexError):
        return info
    info.exists = True
    state = data.get("State", {})
    info.running = bool(state.get("Running"))
    info.status = state.get("Status")
    info.started_at = state.get("StartedAt")
    info.image = data.get("Config", {}).get("Image")
    labels = data.get("Config", {}).get("Labels") or {}
    info.managed_by_circuitloop = labels.get(MANAGED_LABEL) == "true"
    for m in data.get("Mounts", []):
        if m.get("Destination") == s.vivado_container_home:
            info.home_mount_source = m.get("Source")
    return info


async def container_exec(
    container: str, script: str, *, user: str = "user", workdir: str | None = None,
    timeout_s: float = 60, on_line=None, cancel=None, settings: Settings | None = None,
):
    """Run a fixed, CircuitLoop-authored bash snippet inside the container.

    `script` must be built only from validated constants (paths we created,
    version strings matching _VERSION_RE, build ids matching a strict regex).
    """
    s = settings or get_settings()
    argv = [s.docker_bin, "exec", "-u", user]
    if workdir:
        argv += ["-w", workdir]
    argv += [container, "bash", "-c", script]
    return await run_command(argv, timeout_s=timeout_s, on_line=on_line, cancel=cancel)


async def detect(settings: Settings | None = None) -> VivadoEnvironment:
    s = settings or get_settings()
    env = VivadoEnvironment(docker_cli=s.docker_bin)
    env.workspace_host = str(s.vivado_workspace_host)
    env.workspace_container = s.vivado_workspace_container
    env.host_root_exists = (s.vivado_host_root / "Xilinx").is_dir()
    if not env.host_root_exists:
        env.problems.append(f"Vivado host folder {s.vivado_host_root} (with Xilinx/) not found")

    if not s.docker_bin:
        env.docker_error = "Docker CLI not found; install/start Docker Desktop"
        env.problems.append(env.docker_error)
        return env

    r = await _docker(["version", "--format", "{{.Server.Version}}"], timeout_s=15, settings=s)
    if not r.ok:
        env.docker_error = (r.text or r.error or "docker daemon not reachable").strip()[-400:]
        env.problems.append("Docker Desktop is not running")
        return env
    env.docker_daemon = True
    env.docker_server_version = r.output[-1].strip() if r.output else None

    img = await _docker(["image", "inspect", "--format", "{{.Id}}", s.vivado_image], settings=s)
    env.image_present = img.ok

    env.containers = [await _inspect(s.vivado_container, s), await _inspect(s.vivado_headless_container, s)]
    for c in env.containers:  # prefer the user's container, then our headless one
        if c.running:
            if c.home_mount_source and Path(c.home_mount_source).resolve() != s.vivado_host_root.resolve():
                env.problems.append(
                    f"container {c.name} mounts {c.home_mount_source} at {s.vivado_container_home}, "
                    f"expected {s.vivado_host_root}"
                )
                continue
            env.active_container = c.name
            break
    env.can_start_headless = (
        env.image_present and env.host_root_exists and env.active_container is None
        and not any(c.exists for c in env.containers if c.name == s.vivado_headless_container)
    )
    if env.active_container is None:
        env.problems.append("no Vivado container is running")
        return env

    await _probe_inside(env, s)
    return env


async def _probe_inside(env: VivadoEnvironment, s: Settings) -> None:
    home = s.vivado_container_home
    token = secrets.token_hex(8)
    probe_dir = s.vivado_workspace_host / ".probe"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        (probe_dir / "token").write_text(token)
    except OSError as e:
        env.problems.append(f"cannot write shared workspace {probe_dir}: {e}")
        return

    script = (
        f"ls -1d {home}/Xilinx/Vivado/*/ 2>/dev/null | xargs -n1 basename; "
        f"echo '---'; cat {s.vivado_workspace_container}/.probe/token 2>/dev/null; echo; "
        "echo '---'; ps -eo args 2>/dev/null | grep -v grep | grep -E 'lnx64.o/(vivado|hw_server)' || true"
    )
    r = await container_exec(env.active_container, script, timeout_s=30, settings=s)
    if not r.ok:
        env.problems.append(f"docker exec into {env.active_container} failed: {(r.text or r.error or '')[-300:]}")
        return
    sections = r.text.split("---")
    versions = [v.strip() for v in sections[0].split() if _VERSION_RE.match(v.strip())]
    env.vivado_versions = versions
    env.workspace_shared = len(sections) > 1 and sections[1].strip() == token
    if not env.workspace_shared:
        env.problems.append("shared workspace round-trip failed (bind mount mismatch?)")
    procs = sections[2].splitlines() if len(sections) > 2 else []
    for p in procs:
        if "hw_server" in p:
            env.hw_server_running = True
        elif "lnx64.o/vivado" in p:
            if "-mode batch" in p or "-mode tcl" in p:
                if "CLBUILD_" in p:
                    env.batch_builds_running += 1
            else:
                env.gui_session_running = True

    wanted = s.vivado_version if s.vivado_version in versions else (sorted(versions)[-1] if versions else None)
    if not wanted:
        env.problems.append(f"no Vivado installation found under {home}/Xilinx/Vivado")
        return
    env.vivado_version = wanted
    env.vivado_bin = f"{home}/Xilinx/Vivado/{wanted}/bin/vivado"
    env.settings_script = f"{home}/Xilinx/Vivado/{wanted}/settings64.sh"
    chk = await container_exec(
        env.active_container, f"test -x {env.vivado_bin} && test -f {env.settings_script} && echo OK",
        timeout_s=20, settings=s,
    )
    env.vivado_executable = chk.ok and "OK" in chk.text
    if not env.vivado_executable:
        env.problems.append(f"{env.vivado_bin} is not executable in {env.active_container}")


async def start_docker_desktop() -> dict:
    """Ask macOS to launch Docker Desktop (same as header.sh's `open -a Docker`). Approval-gated by caller."""
    r = await run_command(["/usr/bin/open", "-a", "Docker"], timeout_s=30)
    if not r.ok:
        return {"ok": False, "message": r.text or r.error}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        s = get_settings()
        v = await run_command([s.docker_bin or "docker", "version", "--format", "{{.Server.Version}}"], timeout_s=10)
        if v.ok:
            return {"ok": True, "message": f"Docker daemon {v.text.strip()} is up"}
        await asyncio.sleep(3)
    return {"ok": False, "message": "Docker Desktop did not become ready within 120 s"}


async def start_headless(settings: Settings | None = None) -> dict:
    """Start the CircuitLoop-managed headless container. Approval-gated by caller.

    Same image and bind mount as start_container.sh, but: no VNC port, no
    Screen Sharing, no xvcd, no GUI -- the container just idles so batch Vivado
    jobs can be exec'd into it.
    """
    s = settings or get_settings()
    env = await detect(s)
    if env.active_container:
        return {"ok": True, "container": env.active_container, "message": f"{env.active_container} already running; reusing it"}
    if not env.docker_daemon:
        return {"ok": False, "message": "Docker Desktop is not running"}
    if not env.image_present:
        return {"ok": False, "message": f"image {s.vivado_image} not found; build it with the cse100 setup scripts first"}
    if not env.host_root_exists:
        return {"ok": False, "message": f"{s.vivado_host_root} does not contain a Vivado installation"}
    existing = next((c for c in env.containers if c.name == s.vivado_headless_container), None)
    if existing and existing.exists:
        return {"ok": False, "message": f"a stopped container named {s.vivado_headless_container} exists; remove it manually first"}

    argv = [
        s.docker_bin, "run", "-d", "--init", "--rm",
        "--name", s.vivado_headless_container,
        "--label", f"{MANAGED_LABEL}=true",
        "--platform", "linux/amd64",
        "--mount", f"type=bind,source={s.vivado_host_root},target={s.vivado_container_home}",
        s.vivado_image, "sleep", "infinity",
    ]
    r = await run_command(argv, timeout_s=120)
    if not r.ok:
        return {"ok": False, "message": (r.text or r.error or "docker run failed")[-500:]}
    env = await detect(s)
    return {
        "ok": env.ready,
        "container": env.active_container,
        "message": "headless Vivado container started" if env.ready else "; ".join(env.problems),
    }


async def stop_headless(settings: Settings | None = None) -> dict:
    """Stop the headless container -- only if CircuitLoop started it (label check)."""
    s = settings or get_settings()
    info = await _inspect(s.vivado_headless_container, s)
    if not info.exists or not info.running:
        return {"ok": True, "message": "headless container is not running"}
    if not info.managed_by_circuitloop:
        return {"ok": False, "message": f"{info.name} was not started by CircuitLoop; refusing to stop it"}
    r = await _docker(["stop", "-t", "10", info.name], timeout_s=60, settings=s)
    return {"ok": r.ok, "message": r.text.strip() or r.error}
