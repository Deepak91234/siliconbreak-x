"""Trusted board configuration loader (boards/<id>/board.json)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARDS_DIR = REPO_ROOT / "boards"


@dataclass(frozen=True)
class BoardPort:
    name: str
    direction: str  # "input" | "output"
    kind: str
    pins: tuple[str, ...]
    active: str | None = None
    idle: str | None = None

    @property
    def width(self) -> int:
        return len(self.pins)

    @property
    def is_vector(self) -> bool:
        # clk, dp and the five buttons are scalars; everything else is a bus.
        return self.name in ("sw", "led", "seg", "an")


@dataclass(frozen=True)
class Board:
    id: str
    name: str
    part: str
    idcode: str
    openfpgaloader_board: str
    usb_vendor_id: int
    usb_product_id: int
    iostandard: str
    clock_port: str
    clock_hz: int
    clock_period_ns: float
    config: dict
    ports: dict[str, BoardPort]
    source_xdc: Path
    source_xdc_sha256: str

    def verify_source_xdc(self) -> bool:
        return hashlib.sha256(self.source_xdc.read_bytes()).hexdigest() == self.source_xdc_sha256


@lru_cache(maxsize=None)
def load_board(board_id: str = "basys3") -> Board:
    root = BOARDS_DIR / board_id
    data = json.loads((root / "board.json").read_text())
    ports = {
        name: BoardPort(
            name=name,
            direction=p["direction"],
            kind=p["kind"],
            pins=tuple(p["pins"]),
            active=p.get("active"),
            idle=p.get("idle"),
        )
        for name, p in data["ports"].items()
    }
    return Board(
        id=data["id"],
        name=data["name"],
        part=data["part"],
        idcode=data["idcode"],
        openfpgaloader_board=data["openfpgaloader_board"],
        usb_vendor_id=int(data["usb"]["vendor_id"], 16),
        usb_product_id=int(data["usb"]["product_id"], 16),
        iostandard=data["iostandard"],
        clock_port=data["clock"]["port"],
        clock_hz=int(data["clock"]["freq_hz"]),
        clock_period_ns=float(data["clock"]["period_ns"]),
        config=dict(data["config"]),
        ports=ports,
        source_xdc=root / data["source_xdc"],
        source_xdc_sha256=data["source_xdc_sha256"],
    )
