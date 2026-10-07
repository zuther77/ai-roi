"""Naive worker choice. Idle and healthy wins; otherwise alternate.

Day 9's live path is timing.plan_assignment. This function stays so each
assignment can log what the naive rule would have picked. A worker with
no heartbeat is unhealthy (DELL stays parked until it publishes one).
"""

from __future__ import annotations

from dataclasses import dataclass

# Spec names. Order is the tie-break when nothing has been assigned yet.
CANONICAL = ("dell", "macbook_air")


@dataclass(frozen=True)
class WorkerState:
    name: str
    healthy: bool
    busy: bool


def choose_worker(states: list[WorkerState], last_assigned: str | None) -> str | None:
    """Return the worker that should take the next queued item, or None.

    None means every healthy worker is busy, or none are up. The caller
    leaves the item queued and plays filler in the meantime.
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
