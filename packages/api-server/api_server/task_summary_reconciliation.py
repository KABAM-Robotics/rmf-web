"""Persist authoritative terminal transitions from ``/task_summaries``.

The RMF task API response written at dispatch time is only a snapshot. Fleet
adapters publish the eventual task terminal result on ``/task_summaries``.
Keeping the API ledger in sync here is essential because downstream systems
(KTM in Waterloo) use the ledger for lifecycle completion.
"""

from __future__ import annotations

from typing import Protocol

from api_server import models as mdl


class _TaskRepository(Protocol):
    async def get_task_state(self, task_id: str) -> mdl.TaskState | None: ...

    async def save_task_state(self, task_state: mdl.TaskState) -> None: ...


# rmf_task_msgs/TaskSummary constants. Keep numeric values here so this module
# stays unit-testable without a ROS installation.
_TERMINAL_SUMMARY_STATES = {
    2: mdl.TaskStatus.completed,
    3: mdl.TaskStatus.failed,
    4: mdl.TaskStatus.canceled,
}

_TERMINAL_TASK_STATUSES = set(_TERMINAL_SUMMARY_STATES.values()) | {
    mdl.TaskStatus.killed,
    mdl.TaskStatus.skipped,
}


def terminal_status_from_task_summary(state: int) -> mdl.TaskStatus | None:
    """Return a ledger terminal status for an RMF TaskSummary state."""
    return _TERMINAL_SUMMARY_STATES.get(state)


async def reconcile_terminal_task_summary(
    task_repo: _TaskRepository,
    task_id: str,
    summary_state: int,
    finish_unix_millis: int | None,
) -> mdl.TaskState | None:
    """Persist a terminal TaskSummary without ever regressing a ledger state.

    A terminal summary has no detailed phase/event graph. Preserve the latest
    graph already stored by the task API, but terminalize its lifecycle fields.
    ``None`` means the summary is non-terminal, unknown, or already terminal.
    """
    terminal_status = terminal_status_from_task_summary(summary_state)
    if terminal_status is None or not task_id:
        return None

    stored = await task_repo.get_task_state(task_id)
    if stored is None or stored.status in _TERMINAL_TASK_STATUSES:
        return None

    reconciled = stored.model_copy(deep=True)
    reconciled.status = terminal_status
    reconciled.unix_millis_finish_time = finish_unix_millis
    reconciled.active = None
    reconciled.pending = []
    await task_repo.save_task_state(reconciled)
    return reconciled
