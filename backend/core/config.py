"""CircuitLoop configuration.

Values come from the process environment, falling back to a `.env` file in the
repository root (never committed; see .env.example). Secrets such as
ANTHROPIC_API_KEY stay in the backend process and are never sent to the
browser or written into run artifacts.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _which(name: str, *extra_dirs: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in extra_dirs:
        cand = Path(d) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


@dataclass(frozen=True)
class Settings:
    # storage
    runs_dir: Path
    db_path: Path

    # Vivado / Docker (existing cse100-vivado-on-silicon-mac installation)
    docker_bin: str | None
    vivado_container: str            # the user's GUI container started by start_container.sh
    vivado_headless_container: str   # optional headless container CircuitLoop may start (with approval)
    vivado_image: str
    vivado_host_root: Path           # host directory bind-mounted at vivado_container_home
    vivado_container_home: str
    vivado_launcher_dir: Path
    vivado_workspace_subdir: str     # shared build area under the bind mount
    vivado_version: str | None       # None = auto-detect inside the container
    vivado_build_timeout_s: float
    vivado_max_threads: int

    # native tools
    verilator_bin: str | None
    openfpgaloader_bin: str | None

    # simulation
    sim_clk_hz: int

    # agent
    llm_provider: str
    llm_model: str
    anthropic_api_key: str | None = field(repr=False)
    max_repair_attempts: int

    # server
    host: str
    port: int

    @property
    def vivado_workspace_host(self) -> Path:
        return self.vivado_host_root / self.vivado_workspace_subdir

    @property
    def vivado_workspace_container(self) -> str:
        return f"{self.vivado_container_home}/{self.vivado_workspace_subdir}"

    @property
    def llm_configured(self) -> bool:
        return self.llm_provider == "anthropic" and bool(self.anthropic_api_key)

    def public_dict(self) -> dict:
        """Configuration safe to show in the browser (no secrets)."""
        return {
            "runs_dir": str(self.runs_dir),
            "vivado_container": self.vivado_container,
            "vivado_headless_container": self.vivado_headless_container,
            "vivado_image": self.vivado_image,
            "vivado_host_root": str(self.vivado_host_root),
            "vivado_workspace_host": str(self.vivado_workspace_host),
            "vivado_workspace_container": self.vivado_workspace_container,
            "vivado_version": self.vivado_version,
            "verilator_bin": self.verilator_bin,
            "openfpgaloader_bin": self.openfpgaloader_bin,
            "sim_clk_hz": self.sim_clk_hz,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_configured": self.llm_configured,
            "max_repair_attempts": self.max_repair_attempts,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env = {**_load_dotenv(REPO_ROOT / ".env"), **os.environ}

    def get(key: str, default: str) -> str:
        v = env.get(key)
        return v if v not in (None, "") else default

    runs_dir = Path(get("CIRCUITLOOP_RUNS_DIR", str(REPO_ROOT / "runs"))).expanduser()
    vivado_root = Path(get("CIRCUITLOOP_VIVADO_HOST_ROOT", str(HOME / "cse100" / "cse100-vivado-on-silicon-mac-main"))).expanduser()
    return Settings(
        runs_dir=runs_dir,
        db_path=Path(get("CIRCUITLOOP_DB_PATH", str(runs_dir / "circuitloop.sqlite3"))).expanduser(),
        docker_bin=_which("docker", "/usr/local/bin", "/opt/homebrew/bin", "/Applications/Docker.app/Contents/Resources/bin"),
        vivado_container=get("CIRCUITLOOP_VIVADO_CONTAINER", "vivado_container"),
        vivado_headless_container=get("CIRCUITLOOP_VIVADO_HEADLESS_CONTAINER", "circuitloop_vivado"),
        vivado_image=get("CIRCUITLOOP_VIVADO_IMAGE", "x64-linux"),
        vivado_host_root=vivado_root,
        vivado_container_home=get("CIRCUITLOOP_VIVADO_CONTAINER_HOME", "/home/user"),
        vivado_launcher_dir=Path(get("CIRCUITLOOP_VIVADO_LAUNCHER_DIR", str(vivado_root / "scripts"))).expanduser(),
        vivado_workspace_subdir=get("CIRCUITLOOP_VIVADO_WORKSPACE_SUBDIR", "circuitloop_ws"),
        vivado_version=env.get("CIRCUITLOOP_VIVADO_VERSION") or None,
        vivado_build_timeout_s=float(get("CIRCUITLOOP_VIVADO_BUILD_TIMEOUT_S", "2400")),
        vivado_max_threads=int(get("CIRCUITLOOP_VIVADO_MAX_THREADS", "4")),
        verilator_bin=env.get("CIRCUITLOOP_VERILATOR") or _which("verilator", "/opt/homebrew/bin", "/usr/local/bin"),
        openfpgaloader_bin=env.get("CIRCUITLOOP_OPENFPGALOADER")
        or _which("openFPGALoader", "/opt/homebrew/bin", "/usr/local/bin"),
        sim_clk_hz=int(get("CIRCUITLOOP_SIM_CLK_HZ", "10000")),
        llm_provider=get("CIRCUITLOOP_LLM_PROVIDER", "anthropic"),
        llm_model=get("CIRCUITLOOP_LLM_MODEL", "claude-opus-5-5"),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        max_repair_attempts=int(get("CIRCUITLOOP_MAX_REPAIR_ATTEMPTS", "3")),
        host=get("CIRCUITLOOP_HOST", "127.0.0.1"),
        port=int(get("CIRCUITLOOP_PORT", "8000")),
    )
