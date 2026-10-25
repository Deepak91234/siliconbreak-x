"""Host-safety scan for candidate RTL (agent patches, imported sources).

Verilator compiles RTL into C++ and runs it on this machine, so a patched
design is untrusted code. Before any tool sees a candidate file this scan
rejects constructs that could reach the host or forge the verifier's verdict:

  * system tasks outside a small allowlist of pure functions. That bans
    $system, Verilator's inline C++ ($c*), file and plusarg I/O, VCD control,
    and every print/finish task ($display, $write, $finish, ...): the trusted
    testbench owns the simulation's output, and the verifier additionally
    requires a per-run nonce the design cannot read.
  * DPI import/export, `bind`, and compiler directives other than conditional
    compilation and `define (so no `include of host files and no Verilator
    `systemc_* blocks)
  * non-ASCII text and oversized files

It reuses SiliconBreak X's comment-stripping lexer (rtl/safety.py), so banned
names inside comments or strings are ignored and code after a string literal
is still scanned. Anything it cannot lex is rejected.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from backend.rtl.safety import strip_comments, tokenize

MAX_BYTES = 256 * 1024

ALLOWED_SYSTEM_FUNCTIONS = frozenset({
    "$clog2", "$signed", "$unsigned", "$bits", "$size", "$high", "$low", "$left", "$right",
    "$countones", "$onehot", "$onehot0", "$isunknown",
})
ALLOWED_DIRECTIVES = frozenset({
    "default_nettype", "timescale", "define", "undef", "ifdef", "ifndef", "elsif", "else", "endif",
    "resetall", "celldefine", "endcelldefine",
})


@dataclass
class HostSafetyIssue:
    file: str
    line: int
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def scan_source(name: str, text: str) -> list[HostSafetyIssue]:
    issues: list[HostSafetyIssue] = []
    if len(text.encode()) > MAX_BYTES:
        return [HostSafetyIssue(name, 1, f"file is larger than {MAX_BYTES // 1024} KiB")]
    for n, line in enumerate(text.splitlines(), 1):
        if any(ord(c) > 127 for c in line):
            issues.append(HostSafetyIssue(name, n, "non-ASCII characters are not allowed"))
            break
    try:
        code, _ = strip_comments(text)
    except Exception as e:  # unterminated comment/string
        return issues + [HostSafetyIssue(name, getattr(e, "line", 1), f"cannot lex source: {e}")]
    toks = tokenize(code)
    defined = {toks[i + 1].text for i, t in enumerate(toks[:-1]) if t.kind == "directive" and t.text == "`define"}
    for i, t in enumerate(toks):
        if t.kind == "system" and t.text not in ALLOWED_SYSTEM_FUNCTIONS:
            issues.append(HostSafetyIssue(name, t.line, f"system task {t.text} is not allowed in design sources"))
        elif t.kind == "directive":
            d = t.text[1:]
            if d not in ALLOWED_DIRECTIVES and d not in defined:
                issues.append(HostSafetyIssue(name, t.line, f"compiler directive `{d} is not allowed"))
        elif t.kind == "keyword" and t.text in ("bind",):
            issues.append(HostSafetyIssue(name, t.line, "bind is not allowed in design sources"))
        elif t.kind == "keyword" and t.text in ("import", "export") and i + 1 < len(toks) \
                and toks[i + 1].kind == "string" and "DPI" in toks[i + 1].text:
            issues.append(HostSafetyIssue(name, t.line, "DPI import/export is not allowed"))
        elif t.kind == "escaped":
            issues.append(HostSafetyIssue(name, t.line, "escaped identifiers are not allowed"))
    return issues
