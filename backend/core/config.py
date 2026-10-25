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

    # Isolated Vivado container (optional). When set, the Vivado install is a separate,
    # read-only mount at <container home>/Xilinx, and vivado_host_root is a dedicated
    # writable folder instead of the user's Vivado home. CircuitLoop then refuses any
    # container that does not mount the install read-only.
    vivado_install_dir: Path | None = None
    vivado_memory: str = "2560m"
    vivado_cpus: str = "3"

    # SiliconBreak X (security). Keys stay in this process like ANTHROPIC_API_KEY.
    bitdeer_api_key: str | None = field(default=None, repr=False)
    bitdeer_base_url: str = "https://api-inference.bitdeer.ai/v1"
    bitdeer_model: str = "zai-org/GLM-5.3"
    factory_api_key: str | None = field(default=None, repr=False)
    droid_bin: str | None = None
    droid_model: str | None = None
    security_investigator: str = "auto"   # auto | bitdeer | anthropic | rules
    security_repair_model: str = "auto"   # auto | bitdeer | anthropic (model-patch repairs)

    # AI credit protection (backend/core/budget.py). Live calls are off until enabled.
    siliconbreak_live_ai: bool = False
    budget_anthropic_usd: float = 40.0    # of a $50 grant: $10 kept in reserve
    budget_bitdeer_usd: float = 40.0
    budget_daily_usd: float = 15.0        # per provider
    budget_per_call_usd: float = 1.0
    bitdeer_price_in: float = 1.0         # USD / 1M tokens; deliberately high placeholders
    bitdeer_price_out: float = 4.0
    droid_sessions_per_day: int = 6
    droid_max_attempts: int = 2

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
            "vivado_install_dir": str(self.vivado_install_dir) if self.vivado_install_dir else None,
            "verilator_bin": self.verilator_bin,
            "openfpgaloader_bin": self.openfpgaloader_bin,
            "sim_clk_hz": self.sim_clk_hz,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_configured": self.llm_configured,
            "max_repair_attempts": self.max_repair_attempts,
            "bitdeer_configured": bool(self.bitdeer_api_key),
            "bitdeer_model": self.bitdeer_model,
            "factory_configured": bool(self.factory_api_key),
            "droid_bin": self.droid_bin,
            "security_investigator": self.security_investigator,
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
        vivado_install_dir=Path(env["CIRCUITLOOP_VIVADO_INSTALL_DIR"]).expanduser()
        if env.get("CIRCUITLOOP_VIVADO_INSTALL_DIR") else None,
        vivado_memory=get("CIRCUITLOOP_VIVADO_MEMORY", "2560m"),
        vivado_cpus=get("CIRCUITLOOP_VIVADO_CPUS", "3"),
        bitdeer_api_key=env.get("BITDEER_API_KEY") or None,
        bitdeer_base_url=get("BITDEER_BASE_URL", "https://api-inference.bitdeer.ai/v1").rstrip("/"),
        bitdeer_model=get("BITDEER_MODEL", "zai-org/GLM-5.3"),
        factory_api_key=env.get("FACTORY_API_KEY") or None,
        droid_bin=env.get("SILICONBREAK_DROID") or _which("droid", str(HOME / ".local" / "bin"), str(HOME / ".factory" / "bin"),
                                                        "/opt/homebrew/bin", "/usr/local/bin"),
        droid_model=env.get("SILICONBREAK_DROID_MODEL") or None,
        security_investigator=get("SILICONBREAK_INVESTIGATOR", "auto"),
        security_repair_model=get("SILICONBREAK_REPAIR_MODEL", "auto"),
        siliconbreak_live_ai=get("SILICONBREAK_LIVE_AI", "off").strip().lower() in ("1", "on", "true", "yes"),
        budget_anthropic_usd=float(get("SILICONBREAK_BUDGET_ANTHROPIC_USD", "40")),
        budget_bitdeer_usd=float(get("SILICONBREAK_BUDGET_BITDEER_USD", "40")),
        budget_daily_usd=float(get("SILICONBREAK_BUDGET_DAILY_USD", "15")),
        budget_per_call_usd=float(get("SILICONBREAK_BUDGET_PER_CALL_USD", "1.0")),
        bitdeer_price_in=float(get("SILICONBREAK_BITDEER_PRICE_IN", "1.0")),
        bitdeer_price_out=float(get("SILICONBREAK_BITDEER_PRICE_OUT", "4.0")),
        droid_sessions_per_day=int(get("SILICONBREAK_DROID_SESSIONS_PER_DAY", "6")),
        droid_max_attempts=int(get("SILICONBREAK_DROID_MAX_ATTEMPTS", "2")),
    )
