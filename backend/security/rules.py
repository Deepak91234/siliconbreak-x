"""Offline static rules: a deterministic, NOT-AI fallback investigator.

Used when no model provider is configured or a model call fails. Each rule is
a narrow lexical pattern over comment-stripped RTL, labelled as such in every
finding ("offline static rules"), and every hit is only `suspected` until the
trusted verifier reproduces it. The rules are deliberately few:

  SB-R1  a debug/test/bypass signal ORed into an authorization decision
         (CWE-1191 / CWE-1244)
  SB-R2  a lock bit cleared outside a reset branch (CWE-1231)
  SB-R3  a secret/key register driven straight onto an output (CWE-1258-like)

They miss anything that does not look like these patterns, and they can fire
on safe code; the limitations string on each finding says so.
"""

from __future__ import annotations

import re

from backend.rtl.safety import strip_comments, tokenize

RULESET_ID = "siliconbreak-static-rules-v1"

AUTH_RE = re.compile(r"auth|priv|perm|allow|unlock|secure|access_ok|grant_ok", re.I)
DEBUG_RE = re.compile(r"debug|dbg|jtag|scan|dft|test_?mode|bypass|override|backdoor", re.I)
LOCK_RE = re.compile(r"lock", re.I)
SECRET_RE = re.compile(r"secret|key|otp|fuse|password|token", re.I)
RESET_RE = re.compile(r"rst|reset", re.I)

LIMITS = ("Lexical pattern match by offline static rules (not an AI analysis). It can miss weaknesses that do "
          "not match the pattern and can flag safe code; only the trusted verifier's result counts.")


def _props_matching(contract_props: list[dict], *words: str) -> list[str]:
    hits = [p["id"] for p in contract_props if any(w in p["text"].lower() for w in words)]
    return hits or [p["id"] for p in contract_props]


def scan(files: dict[str, str], contract_props: list[dict]) -> list[dict]:
    findings: list[dict] = []
    for name, text in files.items():
        try:
            code, _ = strip_comments(text)
        except Exception:
            continue
        lines = code.splitlines()
        toks = tokenize(code)

        # SB-R1: inside a parenthesised condition (or an assign RHS), an auth-like and a
        # debug-like identifier joined by a logical/bitwise OR.
        i = 0
        seen_lines: set[int] = set()
        while i < len(toks):
            t = toks[i]
            if t.text in ("if", "assign") or (t.kind == "op" and t.text in ("=", "<=") and i > 0):
                end_tok = ")" if t.text == "if" else ";"
                j, depth, idents, ors = i + 1, 0, [], False
                while j < len(toks) and j < i + 80:
                    x = toks[j]
                    if x.text == "(":
                        depth += 1
                    elif x.text == ")":
                        depth -= 1
                        if end_tok == ")" and depth == 0:
                            break
                    elif x.text == ";" and end_tok == ";":
                        break
                    if x.kind == "ident":
                        idents.append(x.text)
                    if x.text in ("||", "|"):
                        ors = True
                    j += 1
                auth = [n for n in idents if AUTH_RE.search(n)]
                dbg = [n for n in idents if DEBUG_RE.search(n) and not AUTH_RE.search(n)]
                if ors and auth and dbg and t.line not in seen_lines:
                    seen_lines.add(t.line)
                    findings.append({
                        "rule": "SB-R1",
                        "title": f"Debug/test signal '{dbg[0]}' can satisfy the '{auth[0]}' access check",
                        "weakness_class": "CWE-1191",
                        "file": name, "line_start": t.line, "line_end": min(len(lines), t.line + 4),
                        "violated_properties": _props_matching(contract_props, "debug", "authoriz"),
                        "severity_reasoning": "If the debug/test input can be driven by an unauthorized party, it "
                                              "replaces the authorization check for whatever this condition guards.",
                        "hypothesis": f"With {auth[0]} = 0 and {dbg[0]} = 1 the guarded access is still granted.",
                        "proposed_test": f"Drive {auth[0]} = 0, {dbg[0]} = 1 and request the protected resource; "
                                         "expect denial and no data.",
                        "confidence": "medium",
                        "limitations": LIMITS,
                    })
                i = j if j > i else i + 1
                continue
            i += 1

        # SB-R2: `<lock> <= 0` with no reset term in the closest enclosing `if` above it.
        for n, ln in enumerate(lines, 1):
            m = re.search(r"\b(\w*lock\w*)\s*<=\s*(1'b0|0|1'h0)\b", ln, re.I)
            if not m:
                continue
            ctx = " ".join(lines[max(0, n - 6):n - 1])
            if RESET_RE.search(ctx):
                continue
            findings.append({
                "rule": "SB-R2",
                "title": f"Lock bit '{m.group(1)}' can be cleared outside reset",
                "weakness_class": "CWE-1231",
                "file": name, "line_start": n, "line_end": n,
                "violated_properties": _props_matching(contract_props, "lock"),
                "severity_reasoning": "A lock that software or an external input can clear does not protect "
                                      "the registers it guards.",
                "hypothesis": f"After {m.group(1)} is set, a later write or input clears it without a reset.",
                "proposed_test": f"Set {m.group(1)}, then attempt to clear it and write the protected register.",
                "confidence": "low",
                "limitations": LIMITS,
            })

        # SB-R3: continuous assignment of a secret-like signal to another net.
        for n, ln in enumerate(lines, 1):
            m = re.match(r"\s*assign\s+(\w+)(?:\s*\[[^\]]*\])?\s*=\s*(\w+)\s*;", ln)
            if m and SECRET_RE.search(m.group(2)) and not SECRET_RE.search(m.group(1)):
                findings.append({
                    "rule": "SB-R3",
                    "title": f"Secret-like signal '{m.group(2)}' drives '{m.group(1)}' without a guard",
                    "weakness_class": "CWE-1258",
                    "file": name, "line_start": n, "line_end": n,
                    "violated_properties": _props_matching(contract_props, "secret", "key", "leak"),
                    "severity_reasoning": "An unguarded copy of secret material is visible wherever that net goes.",
                    "hypothesis": f"{m.group(1)} exposes {m.group(2)} regardless of permission.",
                    "proposed_test": f"Observe {m.group(1)} without authorization; expect no secret value.",
                    "confidence": "low",
                    "limitations": LIMITS,
                })
    return findings
