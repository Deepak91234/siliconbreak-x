#!/usr/bin/env python
"""Live smoke test of the configured LLM provider (makes real, billed API calls).

Usage:
    .venv/bin/python scripts/llm_smoke.py            # intent for Demo 1 only
    .venv/bin/python scripts/llm_smoke.py --rtl      # also generate cl_core with the LLM

Exit codes: 0 success, 2 no provider configured (ANTHROPIC_API_KEY unset), 1 failure.
Nothing here is simulated: without a key the script says so and stops. The API key
is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.agent.intent import interpret_request  # noqa: E402
from backend.agent.llm import LLMError, get_provider  # noqa: E402
from backend.agent.rtlgen import generate_core_rtl  # noqa: E402
from backend.rtl.safety import has_errors  # noqa: E402

DEMO1 = ("Build a 4-bit counter shown on LEDs 0-3. It should count at one increment per second normally and "
         "three increments per second when I hold BTNU.")


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rtl", action="store_true", help="also ask the LLM to write cl_core for the spec")
    ap.add_argument("--request", default=DEMO1, help="natural-language request to interpret")
    args = ap.parse_args(argv)

    provider = get_provider()
    if provider is None:
        print("No LLM provider configured (ANTHROPIC_API_KEY is not set); nothing to test.")
        return 2
    print(f"provider={provider.name} model={provider.model}")
    try:
        t0 = time.monotonic()
        intent = await interpret_request(args.request, None, provider)
        print(f"intent: kind={intent.kind} interpreter={intent.interpreter} ({time.monotonic() - t0:.1f}s)")
        print(f"message: {intent.message}")
        if intent.spec is None:
            print("no spec returned; reasons:", intent.unsupported_reasons)
            return 1 if intent.interpreter == "offline-rules" else 0
        print(json.dumps(intent.spec.model_dump(), indent=2))
        if intent.interpreter == "offline-rules":
            print("NOTE: the LLM call failed and the offline rules answered (see message above).")
            return 1
        if args.rtl:
            t0 = time.monotonic()
            gen = await generate_core_rtl(intent.spec, provider)
            print(f"rtl: generator={gen.generator} lines={len(gen.verilog.splitlines())} "
                  f"safety_errors={sum(1 for s in gen.safety if s.get('severity') == 'error')} "
                  f"({time.monotonic() - t0:.1f}s)")
            print(f"explanation: {gen.explanation}")
            if has_errors(gen.safety) or not gen.generator.startswith("llm:"):
                return 1
    except LLMError as e:
        print(f"LLM error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
