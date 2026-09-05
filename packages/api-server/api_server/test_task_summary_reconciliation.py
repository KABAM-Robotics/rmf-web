import asyncio

from api_server import models as mdl
from api_server.models.rmf_api.task_state import Booking
from api_server.task_summary_reconciliation import (
    reconcile_terminal_task_summary,
    terminal_status_from_task_summary,
)


def make_task_state(task_id):
    return mdl.TaskState(booking=Booking(id=task_id))


class _TaskRepo:
    def __init__(self, state):
        self.state = state
        self.saved = []

    async def get_task_state(self, task_id):
        return self.state if self.state and self.state.booking.id == task_id else None

    async def save_task_state(self, state):
        self.state = state
        self.saved.append(state)


def test_only_terminal_summary_states_are_mapped():
    assert terminal_status_from_task_summary(1) is None
    assert terminal_status_from_task_summary(2) == mdl.TaskStatus.completed
    assert terminal_status_from_task_summary(3) == mdl.TaskStatus.failed
    assert terminal_status_from_task_summary(4) == mdl.TaskStatus.canceled


def test_terminal_summary_closes_underway_ledger_without_rewriting_history():
    state = make_task_state("summary-complete")
    state.status = mdl.TaskStatus.underway
    state.active = 2
    state.pending = [3]
    repo = _TaskRepo(state)

    reconciled = asyncio.run(
        reconcile_terminal_task_summary(repo, "summary-complete", 2, 123456)
    )

    assert reconciled is not None
    assert reconciled.status == mdl.TaskStatus.completed
    assert reconciled.unix_millis_finish_time == 123456
    assert reconciled.active is None
    assert reconciled.pending == []
    assert len(repo.saved) == 1


def test_terminal_summary_never_regresses_a_terminal_ledger():
    state = make_task_state("already-cancelled")
    state.status = mdl.TaskStatus.canceled
    repo = _TaskRepo(state)

    assert asyncio.run(
        reconcile_terminal_task_summary(repo, "already-cancelled", 2, 123456)
    ) is None
    assert repo.saved == []
