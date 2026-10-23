"""Naive worker choice: idle and healthy wins, otherwise alternate.

The live path is timing.plan_assignment; this stays so each assignment can log
what the naive rule would have picked. A worker with no heartbeat is unhealthy.
"""

from __future__ import annotations

from dataclasses import dataclass

# Order is the tie-break when nothing has been assigned yet.
CANONICAL = ("windows", "mac")


@dataclass(frozen=True)
class WorkerState:
    name: str
    healthy: bool
    busy: bool


def choose_worker(states: list[WorkerState], last_assigned: str | None) -> str | None:
    """The worker that should take the next queued item, or None.

    None means every healthy worker is busy, or none are up; the caller leaves
    the item queued and plays filler in the meantime.
    """
    by_name = {s.name: s for s in states}
    available = [
        name for name in CANONICAL
        if name in by_name and by_name[name].healthy and not by_name[name].busy
    ]
    if not available:
        return None
    if last_assigned in available and len(available) > 1:
        available = [name for name in available if name != last_assigned]
    return available[0]
