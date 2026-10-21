"""Scripted test double for the LLMProvider protocol (tests only; no network)."""

from __future__ import annotations

import json
from typing import Any

from backend.agent.llm.base import LLMError, LLMResult, parse_structured_text


class FakeProvider:
    name = "fake"

    def __init__(self, responses: list[Any], model: str = "fake-model") -> None:
        self.model = model
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def structured(self, *, purpose: str, system: str, messages: list[dict], output_model, max_tokens: int = 16000):
        self.calls.append({"purpose": purpose, "system": system, "messages": [dict(m) for m in messages],
                           "output_model": output_model, "max_tokens": max_tokens})
        assert messages and messages[-1]["role"] == "user"
        if not self.responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        text = r if isinstance(r, str) else json.dumps(r)
        value = parse_structured_text(text, output_model, provider=self.name, model=self.model)
        return LLMResult[output_model](value=value, text=text, model=self.model, provider=self.name,
                                       stop_reason="end_turn")


def llm_error(kind: str, message: str = "scripted failure", **kw) -> LLMError:
    return LLMError(kind, message, provider="fake", model="fake-model", **kw)
