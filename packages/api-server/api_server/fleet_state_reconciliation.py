from api_server import models as mdl
from api_server.repositories import TaskRepository


_TERMINAL_TASK_STATUSES = {
    mdl.TaskStatus.canceled,
    mdl.TaskStatus.killed,
    mdl.TaskStatus.completed,
    mdl.TaskStatus.failed,
    mdl.TaskStatus.skipped,
}


async def reconcile_terminal_task_assignments(
    fleet_state: mdl.FleetState,
    task_repo: TaskRepository,
) -> tuple[mdl.FleetState, bool]:
    """Remove stale robot task IDs whose RMF ledger entry is terminal.

    Fleet-state messages are robot telemetry snapshots and can retain the last
    task UUID after the task ledger has already reached a terminal state. The
    ledger is authoritative for lifecycle state; pose, battery, commission and
    issue telemetry remain authoritative in the fleet snapshot.
    """
    if not fleet_state.robots:
        return fleet_state, False

    task_ids = sorted(
        {
            robot.task_id
            for robot in fleet_state.robots.values()
            if robot.task_id
        }
    )
    if not task_ids:
        return fleet_state, False

    states = await task_repo.query_task_states(task_id=task_ids)
    terminal_ids = {
        state.booking.id
        for state in states
        if state.status in _TERMINAL_TASK_STATUSES
    }
    if not terminal_ids:
        return fleet_state, False

    reconciled = fleet_state.model_copy(deep=True)
    changed = False
    for robot in (reconciled.robots or {}).values():
        if robot.task_id not in terminal_ids:
            continue
        robot.task_id = ""
        if robot.status == mdl.RobotStatus.working:
            robot.status = mdl.RobotStatus.idle
        changed = True

    return reconciled, changed
