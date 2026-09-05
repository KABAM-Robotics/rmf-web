import asyncio

from api_server import models as mdl
from api_server.fleet_state_reconciliation import (
    reconcile_terminal_task_assignments,
)
from api_server.test import make_fleet_state, make_task_state


class _TaskRepo:
    def __init__(self, states):
        self.states = states
        self.queries = []

    async def query_task_states(self, task_id=None, **_kwargs):
        self.queries.append(task_id)
        return [state for state in self.states if state.booking.id in (task_id or [])]


def test_terminal_uuid_is_cleared_with_one_batch_query():
    terminal = make_task_state("done-task")
    terminal.status = mdl.TaskStatus.canceled
    active = make_task_state("active-task")
    active.status = mdl.TaskStatus.underway
    repo = _TaskRepo([terminal, active])

    fleet = make_fleet_state()
    fleet.robots = {
        "stale": mdl.RobotState(
            name="stale", status=mdl.RobotStatus.working, task_id="done-task"
        ),
        "active": mdl.RobotState(
            name="active", status=mdl.RobotStatus.working, task_id="active-task"
        ),
    }

    reconciled, changed = asyncio.run(
        reconcile_terminal_task_assignments(fleet, repo)
    )

    assert changed is True
    assert repo.queries == [["active-task", "done-task"]]
    assert reconciled.robots["stale"].task_id == ""
    assert reconciled.robots["stale"].status == mdl.RobotStatus.idle
    assert reconciled.robots["active"].task_id == "active-task"
    assert reconciled.robots["active"].status == mdl.RobotStatus.working
    # Reconciliation never mutates the original telemetry object.
    assert fleet.robots["stale"].task_id == "done-task"
