"""VCD -> JSON for the waveform viewer.

Reads a VCD written by the Verilator harness (one time unit = half a clock period:
rising edge k at t = 2k, the falling edge before it at t = 2k - 1) and returns the
value changes of the selected signals inside an optional time window.
"""

from __future__ import annotations

from pathlib import Path


class VcdError(ValueError):
    pass


def _fmt(raw: str, width: int) -> str:
    """VCD value text -> '0x..' hex, or 'x' / 'z' if any bit is unknown / floating."""
    s = raw.lower()
    if any(ch in s for ch in "xuw-"):
        return "x"
    if "z" in s:
        return "z"
    try:
        return hex(int(s, 2)) if s else "0x0"
    except ValueError:
        return "x"


def _matches(name: str, wanted: set[str]) -> bool:
    if name in wanted:
        return True
    return any(name.endswith("." + w) for w in wanted)


def vcd_to_json(vcd: Path, signals: list[str] | None = None, start: int | None = None,
                end: int | None = None, max_changes_per_signal: int = 4000) -> dict:
    """Parse `vcd`; `signals` are full hierarchical names (or dotted suffixes such as 'u_core.value').

    Times (`start`, `end`, change times) are in VCD units (half cycles). The value of
    every selected signal at `start` is reported as its first change (at t = start).
    """
    path = Path(vcd)
    if not path.is_file():
        raise VcdError(f"no such VCD file: {path.name}")
    wanted = {s.strip() for s in signals or [] if s and s.strip()} or None

    timescale = ""
    scope: list[str] = []
    by_code: dict[str, list[dict]] = {}
    order: list[dict] = []
    seen_names: set[str] = set()
    in_defs = True
    t = 0
    t_first: int | None = None
    t_last = 0
    pending_ts = False

    lo = start if start is not None else None
    hi = end if end is not None else None

    def record(code: str, raw: str) -> None:
        for sig in by_code.get(code, ()):
            val = _fmt(raw, sig["width"])
            if lo is not None and t < lo:
                sig["_before"] = val
                continue
            if hi is not None and t > hi:
                continue
            ch = sig["changes"]
            if ch and ch[-1][1] == val:
                continue
            if len(ch) >= max_changes_per_signal:
                sig["truncated"] = True
                continue
            ch.append([t, val])

    with path.open("r", errors="replace") as fh:
        for line in fh:
            if in_defs:
                tok = line.split()
                if not tok:
                    continue
                if pending_ts:
                    if tok[0] != "$end":
                        timescale += " ".join(x for x in tok if x != "$end")
                    if "$end" in tok:
                        pending_ts = False
                    continue
                kw = tok[0]
                if kw == "$timescale":
                    rest = [x for x in tok[1:] if x != "$end"]
                    timescale = " ".join(rest)
                    pending_ts = "$end" not in tok
                elif kw == "$scope" and len(tok) >= 3:
                    scope.append(tok[2])
                elif kw == "$upscope":
                    if scope:
                        scope.pop()
                elif kw == "$var" and len(tok) >= 5:
                    width, code, ref = int(tok[2]), tok[3], tok[4]
                    name = ".".join(scope + [ref])
                    if name in seen_names or (wanted is not None and not _matches(name, wanted)):
                        continue
                    seen_names.add(name)
                    sig = {"name": name, "width": width, "changes": []}
                    by_code.setdefault(code, []).append(sig)
                    order.append(sig)
                elif kw == "$enddefinitions":
                    in_defs = False
                continue
            s = line.strip()
            if not s:
                continue
            c = s[0]
            if c == "#":
                try:
                    t = int(s[1:])
                except ValueError as e:
                    raise VcdError(f"bad timestamp {s!r}") from e
                if t_first is None:
                    t_first = t
                t_last = max(t_last, t)
                if hi is not None and t > hi:
                    break
            elif c in "01xXzZuU":
                record(s[1:], c)
            elif c in "bBrR":
                parts = s[1:].split()
                if len(parts) == 2:
                    record(parts[1], parts[0] if c in "bB" else "x")
            # $dumpvars / $end / $comment lines carry no values of their own

    w_start = lo if lo is not None else (t_first or 0)
    w_end = hi if hi is not None else t_last
    out = []
    for sig in order:
        before = sig.pop("_before", None)
        if before is not None and (not sig["changes"] or sig["changes"][0][0] > w_start):
            sig["changes"].insert(0, [w_start, before])
        out.append(sig)
    return {"timescale": timescale or "1ps", "time_unit": "half_cycle", "start": int(w_start),
            "end": int(max(w_start, w_end)), "signals": out}
