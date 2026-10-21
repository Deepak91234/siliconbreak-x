"""Static safety checker for generated `cl_core` Verilog.

LLM-written RTL is untrusted input to Verilator (which compiles it to C++ and
runs it on this machine) and to Vivado. Before either tool sees it, this module
rejects constructs that could touch the host, break the trusted wrapper's
assumptions, or make simulation disagree with synthesis:

  * system tasks other than pure constant functions ($clog2/$signed/$unsigned),
    which covers $system, file I/O, $readmem*, $finish, $dumpfile, Verilator's
    inline-C++ `$c`, ...
  * compiler directives other than `default_nettype / `timescale (no `include,
    `define or macro use), DPI import/export, escaped identifiers
  * anything but exactly one module named cl_core whose ANSI header has the
    parameter names of core_parameters(spec) and the ports of core_ports(spec)
  * edge-triggered logic on anything but `posedge clk` (derived/gated clocks,
    negedge, asynchronous resets), clocks other than `clk` wired into library
    instances, instances of modules outside the preverified library
  * initial blocks, delays, event controls inside statements, other
    non-synthesizable or SystemVerilog-only constructs (Vivado reads .v files as
    Verilog-2001, even though Verilator would accept SystemVerilog)
  * translate_off pragmas (code that only one tool sees), non-ASCII text,
    sources over 64 KiB
  * combinational always blocks that do not assign every target on every path
    (best-effort latch heuristic -> warning)

The scanner strips comments with a string-aware lexer first, so a `$system`
inside a comment is ignored while `"// not a comment"` inside a string literal
does not hide the code after it. It is a conservative filter, not a Verilog
parser: anything it cannot classify confidently is left to Verilator lint, which
always runs afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel

from backend.rtl.wrapper import CORE_MODULE, LIBRARY_FILES, core_parameters, core_ports
from backend.specifications.ir import DesignSpec

MAX_SOURCE_BYTES = 64 * 1024

ALLOWED_SYSTEM_FUNCTIONS = frozenset({"$clog2", "$signed", "$unsigned"})
ALLOWED_DIRECTIVES = frozenset({"default_nettype", "timescale"})
LIBRARY_MODULES = frozenset(f.removesuffix(".v") for f in LIBRARY_FILES)
ALLOWED_LINT_OFF = frozenset({"UNUSEDSIGNAL", "UNUSEDPARAM", "UNUSED"})

_HOST_ACCESS_TASKS = frozenset({
    "$system", "$fopen", "$fclose", "$fwrite", "$fwriteb", "$fwriteh", "$fwriteo", "$fdisplay", "$fstrobe",
    "$fmonitor", "$fgets", "$fgetc", "$fscanf", "$fread", "$fflush", "$feof", "$readmemh", "$readmemb",
    "$writememh", "$writememb", "$dumpfile", "$dumpvars", "$dumpall", "$dumpflush", "$dumpon", "$dumpoff",
    "$dumplimit", "$value$plusargs", "$test$plusargs", "$c", "$cpure", "$sformat", "$swrite",
})

VERILOG_KEYWORDS = frozenset("""
always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos config deassign default defparam
design disable edge else end endcase endconfig endfunction endgenerate endmodule endprimitive endspecify endtable
endtask event for force forever fork function generate genvar highz0 highz1 if ifnone incdir include initial inout
input instance integer join large liblist library localparam macromodule medium module nand negedge nmos nor
noshowcancelled not notif0 notif1 or output parameter pmos posedge primitive pull0 pull1 pulldown pullup
pulsestyle_onevent pulsestyle_ondetect rcmos real realtime reg release repeat rnmos rpmos rtran rtranif0 rtranif1
scalared showcancelled signed small specify specparam strong0 strong1 supply0 supply1 table task time tran
tranif0 tranif1 tri tri0 tri1 triand trior trireg unsigned use vectored wait wand weak0 weak1 while wire wor xnor
xor
""".split())

SYSTEMVERILOG_KEYWORDS = frozenset("""
logic bit byte int shortint longint shortreal always_ff always_comb always_latch typedef enum struct union packed
interface endinterface modport package endpackage import export class endclass program endprogram unique unique0
priority assert assume cover property endproperty sequence endsequence final void chandle string bind clocking
endclocking constraint rand randc virtual extern static const ref var type return break continue do foreach
inside dist with new this super local protected pure context forkjoin join_any join_none
""".split())

_NON_SYNTH_ERRORS = {
    "fork": "fork/join is not synthesizable",
    "join": "fork/join is not synthesizable",
    "forever": "forever loops are not synthesizable",
    "wait": "wait statements are not synthesizable",
    "force": "force/release is not synthesizable",
    "release": "force/release is not synthesizable",
    "deassign": "procedural assign/deassign is not synthesizable",
    "defparam": "defparam is not allowed (override parameters in the instance instead)",
    "specify": "specify blocks are not allowed",
    "primitive": "user-defined primitives are not allowed",
    "table": "user-defined primitives are not allowed",
    "config": "configurations are not allowed",
    "library": "library declarations are not allowed",
}
_NON_SYNTH_WARNINGS = {
    "real": "real-valued variables are not synthesizable",
    "realtime": "realtime is not synthesizable",
    "time": "time variables are not synthesizable",
    "event": "named events are not synthesizable",
    "task": "tasks are discouraged in generated cores (use functions or plain logic)",
}

KEYWORDS = VERILOG_KEYWORDS | SYSTEMVERILOG_KEYWORDS


class SafetyIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    line: Optional[int] = None


def has_errors(issues: list[SafetyIssue] | list[dict]) -> bool:
    for i in issues:
        sev = i.get("severity") if isinstance(i, dict) else i.severity
        if sev == "error":
            return True
    return False


def format_issues(issues: list[SafetyIssue]) -> str:
    """One line per issue, for prompts and logs."""
    return "\n".join(f"- [{i.severity}] {i.code}" + (f" (line {i.line})" if i.line else "") + f": {i.message}"
                     for i in issues)


# =============================================================================== lexer


@dataclass(frozen=True)
class Token:
    kind: str  # ident | keyword | system | directive | number | string | op | escaped | other
    text: str
    line: int


@dataclass(frozen=True)
class Comment:
    text: str
    line: int


class _LexError(Exception):
    def __init__(self, message: str, line: int) -> None:
        super().__init__(message)
        self.line = line


def strip_comments(src: str) -> tuple[str, list[Comment]]:
    """Blank out comments (keeping newlines so line numbers survive).

    String literals are copied verbatim and their contents are never treated as
    comment starts; comment contents are never treated as string starts.
    """
    out: list[str] = []
    comments: list[Comment] = []
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            comments.append(Comment(src[i + 2:j], line))
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j == -1:
                raise _LexError("unterminated /* comment", line)
            body = src[i + 2:j]
            comments.append(Comment(body, line))
            chunk = src[i:j + 2]
            out.append("".join("\n" if ch == "\n" else " " for ch in chunk))
            line += chunk.count("\n")
            i = j + 2
        elif c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                if src[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if src[j] == "\n":
                    raise _LexError("unterminated string literal", line)
                j += 1
            if j >= n:
                raise _LexError("unterminated string literal", line)
            out.append(src[i:j + 1])
            i = j + 1
        else:
            if c == "\n":
                line += 1
            out.append(c)
            i += 1
    return "".join(out), comments


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>[ \t\r\f\v]+|\n)
  | (?P<escaped>\\\S+)
  | (?P<directive>`[A-Za-z_]\w*)
  | (?P<system>\$[A-Za-z_][\w$]*)
  | (?P<number>
        (?:\d[\d_]*\s*)?'[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ?_]+
      | '[01xXzZ](?![\w'])
      | \d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?
    )
  | (?P<ident>[A-Za-z_][\w$]*)
  | (?P<string>"(?:\\.|[^"\\\n])*")
  | (?P<op><<<=|>>>=|===|!==|<<<|>>>|<<=|>>=|==|!=|<=|>=|&&|\|\||\*\*|<<|>>|~&|~\||~\^|\^~|->|\+:|-:|::|\+\+|--|\+=|-=|.)
    """,
    re.VERBOSE,
)


def tokenize(code: str) -> list[Token]:
    tokens: list[Token] = []
    line = 1
    for m in _TOKEN_RE.finditer(code):
        kind = m.lastgroup or "other"
        text = m.group()
        if kind == "ws":
            line += text.count("\n")
            continue
        if kind == "ident" and text in KEYWORDS:
            kind = "keyword"
        tokens.append(Token(kind, text, line))
        line += text.count("\n")  # multi-line numbers like 4' b0 are rare but keep counting honest
    return tokens


# =============================================================================== checker


class _Unparsable(Exception):
    pass


class _Checker:
    def __init__(self, src: str, spec: DesignSpec) -> None:
        self.src = src
        self.spec = spec
        self.issues: list[SafetyIssue] = []
        self._seen: set[tuple[str, str, Optional[int]]] = set()

    def add(self, severity: str, code: str, message: str, line: int | None = None) -> None:
        key = (code, message, line)
        if key in self._seen:
            return
        self._seen.add(key)
        self.issues.append(SafetyIssue(severity=severity, code=code, message=message, line=line))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ entry
    def run(self) -> list[SafetyIssue]:
        size = len(self.src.encode("utf-8", errors="replace"))
        if size > MAX_SOURCE_BYTES:
            self.add("error", "oversize", f"source is {size} bytes; the limit is {MAX_SOURCE_BYTES} bytes (64 KiB)")
            return self.issues
        self.check_characters()
        try:
            code, comments = strip_comments(self.src)
        except _LexError as e:
            self.add("error", "lex", str(e), e.line)
            return self.issues
        self.check_comments(comments)
        toks = tokenize(code)
        self.check_tokens(toks)
        body = self.check_structure(toks)
        if body is not None:
            start, end = body
            self.check_always_blocks(toks, start, end)
            self.check_instances(toks, start, end)
        return self.issues

    # ------------------------------------------------------------------ characters
    def check_characters(self) -> None:
        reported = 0
        for n, text in enumerate(self.src.split("\n"), start=1):
            bad = [ch for ch in text if ord(ch) > 127]
            if bad and reported < 5:
                self.add("error", "non-ascii",
                         f"non-ASCII character {bad[0]!r} (U+{ord(bad[0]):04X}); generated RTL must be plain ASCII", n)
                reported += 1
            ctrl = [ch for ch in text if ord(ch) < 32 and ch not in "\t\r\f"]
            if ctrl:
                self.add("error", "control-char", f"control character U+{ord(ctrl[0]):04X} in source", n)

    def check_comments(self, comments: list[Comment]) -> None:
        for c in comments:
            t = c.text
            if re.search(r"\b(synopsys|synthesis|pragma)\s+translate_(off|on)\b", t, re.I):
                self.add("error", "translate-pragma",
                         "translate_off/translate_on pragmas hide code from one tool and make simulation disagree "
                         "with synthesis", c.line)
            if re.search(r"\b(synopsys|synthesis)\s+(full_case|parallel_case)\b", t, re.I):
                self.add("warning", "case-pragma",
                         "full_case/parallel_case pragmas cause simulation/synthesis mismatches", c.line)
            for m in re.finditer(r"\bverilator\s+([a-z_]+)(?:\s+([A-Za-z0-9_]+))?", t):
                directive, arg = m.group(1), m.group(2)
                if directive in ("lint_off", "lint_on"):
                    if directive == "lint_off" and (arg is None or arg.upper() not in ALLOWED_LINT_OFF):
                        what = f"verilator lint_off {arg}" if arg else "verilator lint_off (all warnings)"
                        self.add("warning", "lint-suppression",
                                 f"'{what}' suppresses lint findings that should be fixed instead "
                                 f"(only {', '.join(sorted(ALLOWED_LINT_OFF))} may be waived)", c.line)
                else:
                    self.add("warning", "tool-pragma", f"Verilator pragma 'verilator {directive}' is not allowed in "
                             "generated cores", c.line)

    # ------------------------------------------------------------------ token-level bans
    def check_tokens(self, toks: list[Token]) -> None:
        for k, t in enumerate(toks):
            if t.kind == "system":
                if t.text in ALLOWED_SYSTEM_FUNCTIONS:
                    continue
                why = ("can access the host (files, processes or simulator internals)" if t.text in _HOST_ACCESS_TASKS
                       else "is a simulation-only system task/function")
                self.add("error", "system-task",
                         f"{t.text} {why}; only {', '.join(sorted(ALLOWED_SYSTEM_FUNCTIONS))} are allowed", t.line)
            elif t.kind == "directive":
                name = t.text[1:]
                if name not in ALLOWED_DIRECTIVES:
                    detail = {"include": "file inclusion", "define": "macro definition"}.get(name, "compiler directive or macro")
                    self.add("error", "directive",
                             f"`{name} ({detail}) is not allowed; only `default_nettype and `timescale may appear", t.line)
            elif t.kind == "escaped":
                self.add("error", "escaped-identifier", f"escaped identifier {t.text!r} is not allowed", t.line)
            elif t.kind == "keyword":
                if t.text in ("import", "export"):
                    nxt = toks[k + 1] if k + 1 < len(toks) else None
                    if nxt is not None and nxt.kind == "string" and "DPI" in nxt.text.upper():
                        self.add("error", "dpi", f"DPI {t.text} ({nxt.text}) would call host C code from the design", t.line)
                    else:
                        self.add("error", "systemverilog", f"'{t.text}' is SystemVerilog; generated cores must be "
                                 "Verilog-2001", t.line)
                elif t.text == "initial":
                    self.add("error", "initial-block",
                             "initial blocks are not allowed; give power-on values with declaration initializers "
                             "(reg [3:0] q = 4'd0;)", t.line)
                elif t.text in _NON_SYNTH_ERRORS:
                    self.add("error", "non-synthesizable", f"'{t.text}': {_NON_SYNTH_ERRORS[t.text]}", t.line)
                elif t.text in _NON_SYNTH_WARNINGS:
                    self.add("warning", "non-synthesizable", f"'{t.text}': {_NON_SYNTH_WARNINGS[t.text]}", t.line)
                elif t.text in SYSTEMVERILOG_KEYWORDS:
                    self.add("error", "systemverilog",
                             f"'{t.text}' is SystemVerilog; Vivado reads generated cores as Verilog-2001", t.line)
            elif t.kind == "number" and re.fullmatch(r"'[01xXzZ]", t.text):
                self.add("error", "systemverilog", f"unsized fill literal {t.text} is SystemVerilog; use {{W{{1'b0}}}}",
                         t.line)
            elif t.kind == "string":
                self.add("warning", "string-literal", "string literals have no place in a synthesizable core", t.line)
            elif t.kind == "op" and t.text == "#":
                prev = toks[k - 1] if k else None
                if prev is None or prev.kind != "ident":
                    self.add("error", "delay-control",
                             "delay controls (#...) are ignored by synthesis and make simulation disagree with hardware",
                             t.line)
            elif t.kind == "op" and t.text == "." and 0 < k < len(toks) - 1:
                prev, nxt = toks[k - 1], toks[k + 1]
                if prev.kind == "ident" and nxt.kind == "ident":
                    self.add("error", "hierarchical-reference",
                             f"hierarchical reference {prev.text}.{nxt.text} is not allowed; use ports", t.line)

    # ------------------------------------------------------------------ module structure / header
    def check_structure(self, toks: list[Token]) -> tuple[int, int] | None:
        """Exactly one `module cl_core` ... `endmodule`; returns the body token range."""
        mods = [k for k, t in enumerate(toks) if t.kind == "keyword" and t.text in ("module", "macromodule")]
        ends = [k for k, t in enumerate(toks) if t.kind == "keyword" and t.text == "endmodule"]
        for kw in ("interface", "package", "program", "class", "primitive"):
            for t in toks:
                if t.kind == "keyword" and t.text == kw:
                    self.add("error", "module-count", f"'{kw}' declarations are not allowed; the source must contain "
                             f"exactly one module named {CORE_MODULE}", t.line)
        if not mods:
            self.add("error", "module-count", f"no module found; expected exactly one module named {CORE_MODULE}")
            return None
        names = []
        for k in mods:
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            names.append(nxt.text if nxt is not None else "")
            if nxt is None or nxt.text != CORE_MODULE:
                self.add("error", "module-name",
                         f"module '{nxt.text if nxt else ''}' is not allowed; the only module must be {CORE_MODULE} "
                         "(library modules are provided separately)", toks[k].line)
        if len(mods) != 1:
            self.add("error", "module-count",
                     f"found {len(mods)} modules ({', '.join(names)}); the source must contain exactly one module, "
                     f"{CORE_MODULE}", toks[mods[1]].line)
        if len(ends) != len(mods):
            self.add("error", "module-count", f"{len(mods)} module(s) but {len(ends)} endmodule(s)")
        # Keep analysing the cl_core module (or the first one) even when the
        # count is wrong, so a single response reports every problem at once.
        first = next((k for k in mods if k + 1 < len(toks) and toks[k + 1].text == CORE_MODULE), mods[0])
        end = next((e for e in ends if e > first), None)
        if end is None:
            return None
        if len(mods) == 1 and len(ends) == 1:
            self.check_outside(toks, first, end)
        if toks[first].text == "macromodule":
            self.add("error", "module-name", "use 'module', not 'macromodule'", toks[first].line)
        body_start = self.check_header(toks, first + 2, end)
        if body_start is None:
            return None
        return body_start, end

    def check_outside(self, toks: list[Token], mod: int, end: int) -> None:
        directive_lines = {t.line for t in toks if t.kind == "directive"}
        for k, t in enumerate(toks):
            if mod <= k <= end or t.line in directive_lines:
                continue
            self.add("error", "outside-module", f"'{t.text}' outside the module; only `default_nettype/`timescale may "
                     "appear before or after cl_core", t.line)
            return

    def check_header(self, toks: list[Token], k: int, end: int) -> int | None:
        params: list[tuple[str, Optional[int], int]] = []
        if k < end and toks[k].text == "#":
            if k + 1 >= end or toks[k + 1].text != "(":
                self.add("error", "header", "malformed parameter list after 'module cl_core #'", toks[k].line)
                return None
            close = _match(toks, k + 1)
            if close is None or close >= end:
                self.add("error", "header", "unbalanced parameter list", toks[k].line)
                return None
            for item in _split_commas(toks, k + 2, close):
                p = self._parse_param(item)
                if p is not None:
                    params.append(p)
            k = close + 1
        if k >= end or toks[k].text != "(":
            line = toks[k].line if k < len(toks) else None
            self.add("error", "port-list", "cl_core must use the ANSI port header from core_interface(spec)", line)
            return None
        close = _match(toks, k)
        if close is None or close >= end or close + 1 >= len(toks) or toks[close + 1].text != ";":
            self.add("error", "port-list", "malformed port list in the cl_core header", toks[k].line)
            return None
        ports = self._parse_ports(_split_commas(toks, k + 1, close), toks[k].line)
        self.compare_interface(params, ports, toks[k].line)
        return close + 2

    def _parse_param(self, item: list[Token]) -> tuple[str, Optional[int], int] | None:
        if not item:
            return None
        if "=" not in [t.text for t in item]:
            self.add("error", "param-default", "every cl_core parameter needs a default value", item[0].line)
            return None
        eq = [t.text for t in item].index("=")
        names = [t for t in item[:eq] if t.kind == "ident"]
        if not names:
            self.add("error", "header", "cannot read a parameter name in the header", item[0].line)
            return None
        if item[0].text == "localparam":
            self.add("error", "systemverilog", "localparam in the parameter port list is SystemVerilog", item[0].line)
        value = None
        rhs = item[eq + 1:]
        if len(rhs) == 1 and rhs[0].kind == "number" and re.fullmatch(r"\d[\d_]*", rhs[0].text):
            value = int(rhs[0].text.replace("_", ""))
        return names[-1].text, value, item[0].line

    def _parse_ports(self, items: list[list[Token]], line: int) -> list[tuple[str, str, Optional[int], int]]:
        ports: list[tuple[str, str, Optional[int], int]] = []
        direction: str | None = None
        for item in items:
            if not item:
                continue
            k = 0
            if item[0].text in ("input", "output", "inout"):
                direction = item[0].text
                k = 1
            elif direction is None:
                self.add("error", "port-list",
                         "non-ANSI port list; declare each port with its direction in the header exactly as "
                         "core_interface(spec) shows", item[0].line)
                return ports
            if direction == "inout":
                self.add("error", "port-direction", "inout ports are not allowed", item[0].line)
            while k < len(item) and item[k].text in ("wire", "reg"):
                k += 1
            if k < len(item) and item[k].text == "signed":
                self.add("warning", "port-type", "signed ports differ from the interface (unsigned)", item[k].line)
                k += 1
            width: Optional[int] = 1
            if k < len(item) and item[k].text == "[":
                close = _match(item, k)
                if close is None:
                    self.add("error", "port-range", "unbalanced port range", item[k].line)
                    return ports
                rng = item[k + 1:close]
                if (len(rng) == 3 and rng[1].text == ":" and all(t.kind == "number" and t.text.isdigit()
                                                                    for t in (rng[0], rng[2]))):
                    msb, lsb = int(rng[0].text), int(rng[2].text)
                    if lsb != 0:
                        self.add("error", "port-range", f"port range [{msb}:{lsb}] must be [W-1:0]", item[k].line)
                    width = msb - lsb + 1
                else:
                    self.add("error", "port-range",
                             "port ranges must be integer literals [W-1:0] as in core_interface(spec)", item[k].line)
                    width = None
                k = close + 1
            if k >= len(item) or item[k].kind != "ident":
                self.add("error", "port-list", "cannot read a port name in the header", item[0].line)
                continue
            ports.append((direction, item[k].text, width, item[k].line))
        return ports

    def compare_interface(self, params: list[tuple[str, Optional[int], int]],
                          ports: list[tuple[str, str, Optional[int], int]], line: int) -> None:
        exp_params = {name: value for name, value, _ in core_parameters(self.spec)}
        got_params = {name: (value, ln) for name, value, ln in params}
        for name in exp_params:
            if name not in got_params:
                self.add("error", "param-missing", f"parameter {name} from core_interface(spec) is missing", line)
        for name, (value, ln) in got_params.items():
            if name not in exp_params:
                self.add("error", "param-extra", f"parameter {name} is not part of core_interface(spec); "
                         "use a localparam inside the module instead", ln)
            elif value is not None and value != exp_params[name]:
                self.add("warning", "param-default",
                         f"parameter {name} defaults to {value}, core_interface(spec) says {exp_params[name]} "
                         "(the wrapper overrides it, but keep the header identical)", ln)
        exp_ports = {p.name: (p.direction, p.width) for p in core_ports(self.spec)}
        seen: set[str] = set()
        for direction, name, width, ln in ports:
            if name in seen:
                self.add("error", "port-duplicate", f"port {name} is declared twice", ln)
            seen.add(name)
            if name not in exp_ports:
                self.add("error", "port-extra", f"port {name} is not part of core_interface(spec)", ln)
                continue
            exp_dir, exp_w = exp_ports[name]
            if direction != exp_dir:
                self.add("error", "port-direction", f"port {name} must be an {exp_dir}, not an {direction}", ln)
            if width is not None and width != exp_w:
                self.add("error", "port-width", f"port {name} must be {exp_w} bit(s) wide, not {width}", ln)
        for name in exp_ports:
            if name not in seen:
                self.add("error", "port-missing", f"port {name} from core_interface(spec) is missing", line)

    # ------------------------------------------------------------------ always blocks
    def check_always_blocks(self, toks: list[Token], start: int, end: int) -> None:
        ports = {p.name for p in core_ports(self.spec)}
        always_events: set[int] = set()
        k = start
        while k < end:
            t = toks[k]
            if t.kind == "keyword" and t.text in ("always", "always_ff", "always_comb", "always_latch"):
                if t.text == "always_comb":
                    k += 1
                    continue
                if k + 1 >= end or toks[k + 1].text != "@":
                    if t.text == "always":
                        self.add("error", "always-no-event",
                                 "'always' without an event control is not synthesizable; use always @(posedge clk) "
                                 "or always @*", t.line)
                    k += 1
                    continue
                always_events.add(k + 1)
                k = self._check_event(toks, k, end, ports)
                continue
            if t.text == "@" and k not in always_events:
                self.add("error", "event-control", "event controls (@) are only allowed directly after 'always'", t.line)
            k += 1

    def _check_event(self, toks: list[Token], k: int, end: int, ports: set[str]) -> int:
        line = toks[k].line
        j = k + 2  # token after '@'
        if j < end and toks[j].text == "*":
            return self._check_comb(toks, j + 1, end, line, explicit=False)
        if j >= end or toks[j].text != "(":
            self.add("error", "always-event", "malformed event control", line)
            return j
        close = _match(toks, j)
        if close is None:
            self.add("error", "always-event", "unbalanced event control", line)
            return end
        inner = toks[j + 1:close]
        if len(inner) == 1 and inner[0].text == "*":
            return self._check_comb(toks, close + 1, end, line, explicit=False)
        items: list[list[Token]] = [[]]
        for t in inner:
            if t.text in ("or", ","):
                items.append([])
            else:
                items[-1].append(t)
        edges = [it for it in items if it and it[0].text in ("posedge", "negedge")]
        if not edges:
            self.add("warning", "sensitivity-list",
                     "explicit sensitivity lists can miss signals; use always @* for combinational logic", line)
            return self._check_comb(toks, close + 1, end, line, explicit=True)
        texts = [" ".join(t.text for t in it) for it in items if it]
        if texts == ["posedge clk"]:
            return close + 1
        for it in items:
            if not it:
                continue
            desc = " ".join(t.text for t in it)
            sig = it[1].text if len(it) >= 2 else ""
            if it[0].text == "negedge":
                self.add("error", "clock-edge", f"'{desc}': only 'posedge clk' may trigger sequential logic", line)
            elif it[0].text == "posedge" and sig != "clk":
                if sig in ports:
                    self.add("error", "clock-edge",
                             f"'{desc}': inputs must be sampled synchronously; only 'posedge clk' may trigger "
                             "sequential logic (no asynchronous resets or edge-triggered inputs)", line)
                else:
                    self.add("error", "derived-clock",
                             f"'{desc}': '{sig}' is used as a clock; derived or gated clocks are not allowed, use a "
                             "clock enable (cl_tick_div) with posedge clk", line)
            elif it[0].text not in ("posedge", "negedge"):
                self.add("error", "clock-edge", f"mixed edge and level sensitivity '{' or '.join(texts)}'", line)
        if "posedge clk" in texts and len(texts) > 1:
            self.add("error", "clock-edge",
                     f"'@({' or '.join(texts)})': asynchronous controls are not allowed; use synchronous logic on "
                     "posedge clk only", line)
        return close + 1

    def _check_comb(self, toks: list[Token], k: int, end: int, line: int, explicit: bool) -> int:
        try:
            nxt, definite, assigned = _parse_stmt(toks, k, end)
        except _Unparsable:
            return k
        latches = sorted(assigned - definite)
        if latches:
            self.add("error", "latch",
                     f"combinational always block does not assign {', '.join(latches)} on every path; this infers "
                     "a latch (add a default assignment at the top of the block or complete every if/case)", line)
        return nxt

    # ------------------------------------------------------------------ instances
    def check_instances(self, toks: list[Token], start: int, end: int) -> None:
        k = start
        while k < end - 2:
            t = toks[k]
            if t.kind == "ident" and (k == start or toks[k - 1].text in (";", "begin", "end", ")", "endgenerate",
                                                                         "generate", "else", ":", "endcase")):
                nxt = toks[k + 1]
                inst_at = None
                if nxt.text == "#":
                    if k + 2 < end and toks[k + 2].text == "(":
                        close = _match(toks, k + 2)
                        if close is not None and close + 1 < end and toks[close + 1].kind == "ident":
                            inst_at = close + 1
                elif nxt.kind == "ident" and toks[k + 2].text in ("(", "["):
                    inst_at = k + 1
                if inst_at is not None:
                    self._check_instance(toks, k, inst_at, end)
            k += 1

    def _check_instance(self, toks: list[Token], k: int, name_at: int, end: int) -> None:
        module = toks[k].text
        if module == CORE_MODULE:
            self.add("error", "unknown-module", "cl_core must not instantiate itself", toks[k].line)
            return
        if module not in LIBRARY_MODULES:
            self.add("error", "unknown-module",
                     f"instance of '{module}': only the preverified library modules "
                     f"({', '.join(sorted(LIBRARY_MODULES))}) may be instantiated", toks[k].line)
            return
        j = name_at + 1
        if j < end and toks[j].text == "[":
            close = _match(toks, j)
            j = (close + 1) if close is not None else end
        if j >= end or toks[j].text != "(":
            return
        close = _match(toks, j)
        if close is None:
            return
        conns = _split_commas(toks, j + 1, close)
        named = [c for c in conns if c and c[0].text == "."]
        if conns and len(named) != len([c for c in conns if c]):
            self.add("warning", "positional-ports",
                     f"instance of {module} uses positional connections; use named connections (.clk(clk), ...)",
                     toks[k].line)
            return
        for c in named:
            if len(c) >= 2 and c[1].text == "clk":
                inner = [t.text for t in c[3:-1]] if len(c) >= 4 else []
                if inner != ["clk"]:
                    self.add("error", "derived-clock",
                             f"instance of {module} has .clk({' '.join(inner)}); library modules must be clocked by "
                             "clk itself (use clock enables for slower timing)", toks[k].line)


# =============================================================================== helpers

_OPEN = {"(": ")", "[": "]", "{": "}"}


def _match(toks: list[Token], k: int) -> int | None:
    """Index of the bracket closing toks[k]."""
    stack = []
    for j in range(k, len(toks)):
        t = toks[j].text
        if t in _OPEN:
            stack.append(_OPEN[t])
        elif t in (")", "]", "}"):
            if not stack or stack.pop() != t:
                return None
            if not stack:
                return j
    return None


def _split_commas(toks: list[Token], start: int, end: int) -> list[list[Token]]:
    items: list[list[Token]] = [[]]
    depth = 0
    for t in toks[start:end]:
        if t.text in _OPEN:
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
        if t.text == "," and depth == 0:
            items.append([])
        else:
            items[-1].append(t)
    return items if any(items) else []


def _skip_parens(toks: list[Token], k: int, end: int) -> int:
    if k >= end or toks[k].text != "(":
        raise _Unparsable
    close = _match(toks, k)
    if close is None or close >= end:
        raise _Unparsable
    return close + 1


def _parse_stmt(toks: list[Token], k: int, end: int) -> tuple[int, set[str], set[str]]:
    """Parse one procedural statement; returns (next index, definitely assigned, possibly assigned).

    Only the statement forms that matter for latch inference are understood;
    anything else raises _Unparsable and the heuristic stays silent.
    """
    if k >= end:
        raise _Unparsable
    t = toks[k].text
    if t == "begin":
        k += 1
        if k < end and toks[k].text == ":":
            k += 2
        definite: set[str] = set()
        assigned: set[str] = set()
        while k < end and toks[k].text != "end":
            k, d, a = _parse_stmt(toks, k, end)
            definite |= d
            assigned |= a
        if k >= end:
            raise _Unparsable
        return k + 1, definite, assigned
    if t == "if":
        k = _skip_parens(toks, k + 1, end)
        k, d_then, a_then = _parse_stmt(toks, k, end)
        if k < end and toks[k].text == "else":
            k, d_else, a_else = _parse_stmt(toks, k + 1, end)
            return k, d_then & d_else, a_then | a_else
        return k, set(), a_then
    if t in ("case", "casez", "casex"):
        k = _skip_parens(toks, k + 1, end)
        branches: list[set[str]] = []
        assigned = set()
        has_default = False
        while k < end and toks[k].text != "endcase":
            if toks[k].text == "default":
                has_default = True
                k += 1
                if k < end and toks[k].text == ":":
                    k += 1
            else:
                depth = 0
                while k < end:
                    x = toks[k].text
                    if x in _OPEN:
                        depth += 1
                    elif x in (")", "]", "}"):
                        depth -= 1
                    elif x == ":" and depth == 0:
                        break
                    k += 1
                if k >= end:
                    raise _Unparsable
                k += 1
            k, d, a = _parse_stmt(toks, k, end)
            branches.append(d)
            assigned |= a
        if k >= end:
            raise _Unparsable
        definite = set.intersection(*branches) if (branches and has_default) else set()
        return k + 1, definite, assigned
    if t == "for":
        k = _skip_parens(toks, k + 1, end)
        return _parse_stmt(toks, k, end)
    if t in ("while", "repeat"):
        k = _skip_parens(toks, k + 1, end)
        k, _d, a = _parse_stmt(toks, k, end)
        return k, set(), a
    if t == ";":
        return k + 1, set(), set()
    if toks[k].kind == "ident" or t == "{":
        j, depth, eq = k, 0, None
        while j < end:
            x = toks[j].text
            if x in _OPEN:
                depth += 1
            elif x in (")", "]", "}"):
                depth -= 1
            elif depth == 0 and x == ";":
                break
            elif depth == 0 and eq is None and x in ("=", "<="):
                eq = j
            j += 1
        if j >= end:
            raise _Unparsable
        targets: set[str] = set()
        if eq is not None:
            lhs = toks[k:eq]
            if lhs and lhs[0].text == "{":
                depth = 0
                for tk in lhs:
                    if tk.text == "[":
                        depth += 1
                    elif tk.text == "]":
                        depth -= 1
                    elif depth == 0 and tk.kind == "ident":
                        targets.add(tk.text)
            elif lhs:
                targets.add(lhs[0].text)
        return j + 1, set(targets), set(targets)
    raise _Unparsable


def check_core_source(verilog: str, spec: DesignSpec) -> list[SafetyIssue]:
    """All safety findings for a candidate cl_core source (errors block the pipeline)."""
    return _Checker(verilog, spec).run()
