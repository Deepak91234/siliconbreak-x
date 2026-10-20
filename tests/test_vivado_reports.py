"""Report parsers, exercised on real Vivado 2023.2 output from the Demo 1 build."""

from pathlib import Path

from backend.vivado.reports import inspect_bitstream, parse_drc, parse_timing_summary, parse_utilization

FIX = Path(__file__).parent / "fixtures" / "vivado_demo1"


def test_timing_summary():
    t = parse_timing_summary(FIX / "timing_summary.rpt")
    assert t.wns_ns == 5.239 and t.whs_ns == 0.131
    assert t.tns_failing_endpoints == 0 and t.ths_failing_endpoints == 0
    assert t.constraints_met is True and t.timing_met
    assert t.clocks == [{"name": "sys_clk_pin", "period_ns": 10.0, "freq_mhz": 100.0}]


def test_utilization():
    u = parse_utilization(FIX / "utilization_impl.rpt")
    assert u.get("lut").used == 26 and u.get("lut").available == 20800
    assert u.get("ff").used == 55 and u.get("ff").available == 41600
    assert u.get("iob").used == 30 and u.get("iob").available == 106
    assert u.get("bufg").used == 1


def test_drc_clean():
    d = parse_drc(FIX / "drc.rpt")
    assert d.checks_found == 0 and d.errors == 0 and d.critical_warnings == 0


def test_bitstream_header(tmp_path):
    # The fixture holds only the first 4 KiB, so the length check must flag it as truncated.
    info = inspect_bitstream(FIX / "basys3_top.bit.head", "xc7a35tcpg236-1")
    assert info.part == "7a35tcpg236"
    assert info.design_field.startswith("basys3_top;")
    assert info.sync_word_found
    assert not info.valid and "length" in info.problem


def test_bitstream_wrong_part(tmp_path):
    info = inspect_bitstream(FIX / "basys3_top.bit.head", "xc7a100tcsg324-1")
    assert not info.valid and "does not match" in info.problem
