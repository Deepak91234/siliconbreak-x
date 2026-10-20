"""Parsers for Vivado text reports and the .bit file header.

Only facts read from the actual report files are returned; when a value
cannot be found it is reported as None rather than guessed.
"""

from __future__ import annotations

import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

_UTIL_ROW = re.compile(r"^\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<used>\d+)\s*\|\s*(?P<fixed>\d+)\s*\|"
                       r"(?:\s*(?P<prohib>\d+)\s*\|)?\s*(?P<avail>\d+)\s*\|\s*(?P<pct>[<\d.]+)\s*\|")

# Rows surfaced in the dashboard (exact names from report_utilization for 7-series).
KEY_RESOURCES = {
    "Slice LUTs": "lut",
    "Slice LUTs*": "lut",
    "LUT as Logic": "lut_logic",
    "Slice Registers": "ff",
    "Register as Flip Flop": "ff_flipflop",
    "F7 Muxes": "f7_mux",
    "F8 Muxes": "f8_mux",
    "Slice": "slice",
    "Block RAM Tile": "bram",
    "DSPs": "dsp",
    "Bonded IOB": "iob",
    "BUFGCTRL": "bufg",
    "MMCME2_ADV": "mmcm",
    "PLLE2_ADV": "pll",
}


@dataclass
class Resource:
    name: str
    key: str | None
    used: int
    available: int
    percent: float


@dataclass
class Utilization:
    resources: list[Resource] = field(default_factory=list)

    def get(self, key: str) -> Resource | None:
        return next((r for r in self.resources if r.key == key), None)

    def as_dict(self) -> dict:
        return {"resources": [asdict(r) for r in self.resources],
                "summary": {r.key: {"used": r.used, "available": r.available, "percent": r.percent}
                            for r in self.resources if r.key}}


def parse_utilization(path: Path) -> Utilization:
    util = Utilization()
    seen: set[str] = set()
    for line in Path(path).read_text(errors="replace").splitlines():
        m = _UTIL_ROW.match(line)
        if not m:
            continue
        name = m["name"].strip()
        if name in seen or name in ("Site Type", "Ref Name"):
            continue
        seen.add(name)
        pct_s = m["pct"].lstrip("<")
        try:
            pct = float(pct_s)
        except ValueError:
            pct = 0.0
        util.resources.append(Resource(name=name, key=KEY_RESOURCES.get(name), used=int(m["used"]),
                                       available=int(m["avail"]), percent=pct))
    return util


@dataclass
class TimingSummary:
    wns_ns: float | None = None
    tns_ns: float | None = None
    tns_failing_endpoints: int | None = None
    whs_ns: float | None = None
    ths_ns: float | None = None
    ths_failing_endpoints: int | None = None
    wpws_ns: float | None = None
    total_endpoints: int | None = None
    constraints_met: bool | None = None
    clocks: list[dict] = field(default_factory=list)
    unconstrained_note: str | None = None

    @property
    def timing_met(self) -> bool:
        return bool(
            self.constraints_met
            and self.wns_ns is not None and self.wns_ns >= 0
            and self.whs_ns is not None and self.whs_ns >= 0
        )

    def as_dict(self) -> dict:
        d = asdict(self)
        d["timing_met"] = self.timing_met
        return d


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def parse_timing_summary(path: Path) -> TimingSummary:
    t = TimingSummary()
    lines = Path(path).read_text(errors="replace").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("WNS(ns)") and "TNS(ns)" in line:
            # header, dashes, values
            for j in range(i + 1, min(i + 5, len(lines))):
                vals = lines[j].split()
                if vals and not set(vals[0]) <= {"-"}:
                    if len(vals) >= 8:
                        t.wns_ns, t.tns_ns = _num(vals[0]), _num(vals[1])
                        t.tns_failing_endpoints, t.total_endpoints = int(vals[2]), int(vals[3])
                        t.whs_ns, t.ths_ns = _num(vals[4]), _num(vals[5])
                        t.ths_failing_endpoints = int(vals[6])
                        if len(vals) >= 9:
                            t.wpws_ns = _num(vals[8])
                    break
            break
    text = "\n".join(lines)
    if "All user specified timing constraints are met." in text:
        t.constraints_met = True
    elif "Timing constraints are not met." in text:
        t.constraints_met = False
    # Clock Summary table:  Clock  Waveform(ns)  Period(ns)  Frequency(MHz) / dashes / rows / blank
    for i, line in enumerate(lines):
        if line.startswith("Clock") and "Period(ns)" in line and "Frequency(MHz)" in line:
            for row in lines[i + 2:]:
                if not row.strip():
                    break
                parts = row.split()
                if len(parts) >= 4 and _num(parts[-1]) is not None and _num(parts[-2]) is not None:
                    t.clocks.append({"name": parts[0], "period_ns": _num(parts[-2]), "freq_mhz": _num(parts[-1])})
            break
    return t


@dataclass
class DrcSummary:
    checks_found: int | None = None
    errors: int = 0
    critical_warnings: int = 0
    warnings: int = 0
    violations: list[dict] = field(default_factory=list)


def parse_drc(path: Path) -> DrcSummary:
    d = DrcSummary()
    text = Path(path).read_text(errors="replace")
    m = re.search(r"(?:Checks found|Violations found):\s*(\d+)", text)
    if m:
        d.checks_found = int(m.group(1))
    for row in re.finditer(r"^\|\s*([A-Z0-9-]+)\s*\|\s*(Error|Critical Warning|Warning|Advisory)\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|", text, re.M):
        rule, sev, desc, count = row.group(1), row.group(2), row.group(3), int(row.group(4))
        d.violations.append({"rule": rule, "severity": sev, "description": desc, "count": count})
        if sev == "Error":
            d.errors += count
        elif sev == "Critical Warning":
            d.critical_warnings += count
        elif sev == "Warning":
            d.warnings += count
    return d


@dataclass
class BitstreamInfo:
    valid: bool
    size_bytes: int
    design_field: str | None = None
    part: str | None = None
    date: str | None = None
    time: str | None = None
    data_length: int | None = None
    sync_word_found: bool = False
    problem: str | None = None


def inspect_bitstream(path: Path, expected_part: str) -> BitstreamInfo:
    """Parse the Xilinx .bit header (fields a-e) and look for the 0xAA995566 sync word."""
    raw = Path(path).read_bytes()
    info = BitstreamInfo(valid=False, size_bytes=len(raw))
    try:
        (hlen,) = struct.unpack(">H", raw[0:2])
        pos = 2 + hlen
        (one,) = struct.unpack(">H", raw[pos:pos + 2])
        pos += 2
        if one != 1:
            info.problem = "unexpected header layout"
            return info
        fields: dict[str, str] = {}
        while pos < len(raw):
            key = chr(raw[pos])
            pos += 1
            if key == "e":
                (info.data_length,) = struct.unpack(">I", raw[pos:pos + 4])
                pos += 4
                break
            (flen,) = struct.unpack(">H", raw[pos:pos + 2])
            pos += 2
            fields[key] = raw[pos:pos + flen].rstrip(b"\0").decode("ascii", errors="replace")
            pos += flen
        info.design_field = fields.get("a")
        info.part = fields.get("b")
        info.date = fields.get("c")
        info.time = fields.get("d")
        info.sync_word_found = b"\xaa\x99\x55\x66" in raw[pos:pos + 256]
    except (struct.error, IndexError) as e:
        info.problem = f"cannot parse header: {e}"
        return info
    # board.json part is xc7a35tcpg236-1; the .bit header stores "7a35tcpg236"
    want = expected_part.lower().removeprefix("xc").split("-")[0]
    if not info.part or info.part.lower() != want:
        info.problem = f"bitstream part {info.part!r} does not match {want!r}"
    elif not info.sync_word_found:
        info.problem = "configuration sync word not found"
    elif info.data_length is None or info.data_length + pos > len(raw) + 4:
        info.problem = "bitstream data length inconsistent with file size"
    else:
        info.valid = True
    return info


def collect_log_messages(lines: list[str]) -> dict:
    crit = [l for l in lines if l.startswith("CRITICAL WARNING:")]
    errs = [l for l in lines if l.startswith("ERROR:")]
    warns = [l for l in lines if l.startswith("WARNING:")]
    return {"critical_warnings": crit, "errors": errs, "warning_count": len(warns), "warnings_sample": warns[:50]}
