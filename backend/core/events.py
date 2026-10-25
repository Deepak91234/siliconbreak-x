"""In-process event bus for live pipeline updates.

Every observable thing the dashboard shows is an Event published here by the
component that actually did the work (the orchestrator, a tool adapter, the
hardware monitor, the vision analyser). Sinks persist events (SQLite) and the
WebSocket endpoint fans them out to browsers. Nothing synthesises progress.

Event types (data payload documented in docs/api.md):
    run.created run.updated run.finished
    stage.updated            {stage, status, summary?, detail?}
    log                      {stage, source, line}
    agent.message            {role, text, kind?}
    approval.requested       {approval}
    approval.resolved        {approval}
    revision.created         {revision}
    artifact.created         {artifact}
    environment.updated      {environment}
    hardware.updated         {hardware}
    vision.updated           {session_id, ...}
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger("siliconbreak.events")

# Pipeline stages in display order. Keys are part of the API contract.
STAGES: list[tuple[str, str]] = [
    ("spec", "Specification"),
    ("validate", "Capability validation"),
    ("rtl", "Verilog generation"),
    ("lint", "Verilator lint"),
    ("tests", "Test generation"),
    ("simulate", "Simulation"),
    ("repair", "Automatic debugging"),
    ("synth", "Synthesis"),
    ("impl", "Implementation"),
    ("timing", "Timing validation"),
    ("bitstream", "Bitstream"),
    ("detect", "FPGA detection"),
    ("program", "Programming"),
    ("verify", "Physical verification"),
]
STAGE_KEYS = [k for k, _ in STAGES]

# Stage statuses. 'unavailable' = the tool/hardware needed is not present;
# 'inconclusive' = ran but evidence was insufficient; 'blocked' = waiting on
# user approval or user action.
STAGE_STATUSES = (
    "pending", "running", "passed", "failed", "skipped",
    "blocked", "unavailable", "inconclusive", "cancelled",
)


class Event(BaseModel):
    seq: int = 0
    ts: float = Field(default_factory=time.time)
    type: str
    run_id: str | None = None
    project_id: str | None = None
    stage: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


Sink = Callable[[Event], "Awaitable[None] | None"]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._sinks: list[Sink] = []
        self._seq = itertools.count(1)

    def add_sink(self, sink: Sink) -> None:
        if sink not in self._sinks:
            self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def subscribe(self, maxsize: int = 5000) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def set_next_seq(self, value: int) -> None:
        self._seq = itertools.count(value)

    async def publish(self, event: Event) -> Event:
        event.seq = next(self._seq)
        for sink in self._sinks:
            try:
                r = sink(event)
                if inspect.isawaitable(r):
                    await r
            except Exception:  # a broken sink must not break the pipeline
                log.exception("event sink failed")
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # slow client: drop it; it will resync from the REST API
                self._subscribers.discard(q)
        return event

    async def emit(self, type: str, *, run_id: str | None = None, project_id: str | None = None,
                   stage: str | None = None, **data: Any) -> Event:
        return await self.publish(Event(type=type, run_id=run_id, project_id=project_id, stage=stage, data=data))


bus = EventBus()
