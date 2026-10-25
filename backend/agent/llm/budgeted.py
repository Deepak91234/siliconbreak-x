"""LLMProvider wrapper that routes every call through the AI credit guard.

Before the request: reserve the worst-case cost (backend/core/budget.py); a refused
reservation becomes an LLMError, so every caller's existing degraded path (offline
rules, "repair unavailable") handles it. After the request: settle with the usage the
provider reported, or keep the worst case when the request may have been billed.
"""

from __future__ import annotations

import json

from backend.core import budget

from .base import LLMError, LLMResult, T


class BudgetedProvider:
    def __init__(self, inner, provider: str) -> None:
        self.inner = inner
        self.provider = provider
        self.name = inner.name
        self.model = inner.model

    def __repr__(self) -> str:
        return f"BudgetedProvider({self.inner!r})"

    async def structured(self, *, purpose: str, system: str, messages: list[dict], output_model: type[T],
                         max_tokens: int = 16000) -> LLMResult[T]:
        chars = len(system) + sum(len(str(m.get("content", ""))) for m in messages) \
            + len(json.dumps(output_model.model_json_schema()))
        try:
            rid = budget.reserve(self.provider, self.model, purpose, chars, max_tokens)
        except budget.BudgetRefused as e:
            budget.refuse_record(self.provider, self.model, purpose, str(e))
            raise LLMError("api", f"AI credit guard refused the call: {e}", provider=self.name, model=self.model,
                           details={"budget": True}) from None
        try:
            res = await self.inner.structured(purpose=purpose, system=system, messages=messages,
                                              output_model=output_model, max_tokens=max_tokens)
        except LLMError as e:
            budget.settle(rid, ok=False, error_kind=e.kind)
            raise
        except BaseException:
            budget.settle(rid, ok=False, error_kind="network")
            raise
        budget.settle(rid, ok=True, usage=res.usage, model=res.model)
        return res
