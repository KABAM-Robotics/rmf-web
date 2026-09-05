import time
from datetime import datetime
from typing import Annotated, Literal, TypeGuard

from fastapi import Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from reactivex import operators as rxops

from api_server import models as mdl
from api_server.dependencies import pagination_query, time_between_query
from api_server.fast_io import FastIORouter, SubscriptionRequest
from api_server.logging import LoggerAdapter, default_logger, get_logger
from api_server.models.rmf_api.task_state import Cancellation
from api_server.gateway import get_rmf_gateway
from api_server.repositories import FleetRepository, RmfRepository, TaskRepository
from api_server.response import RawJSONResponse
from api_server.rmf_io import RmfService, get_task_events, get_tasks_service
from api_server.routes.building_map import get_building_map

router = FastIORouter(tags=["Tasks"])


class ReconcileRobotResetRequest(BaseModel):
    fleet: str = Field(min_length=1)
    robot: str = Field(min_length=1)
    expected_task_id: str = Field(min_length=1)
    reason: Literal["operator_confirmed_dock_reset"]


class ReconcileRobotResetResponse(BaseModel):
    success: bool
    reconciled: bool
    robot: str
    task_id: str
    task_status: str
    fleet_state_task_id: str


class ReconcileHistoricalCompletionRequest(BaseModel):
    fleet: str = Field(min_length=1)
    robot: str = Field(min_length=1)
    expected_task_id: str = Field(min_length=1)
    reason: Literal["historical_final_phase_completion"]


class ReconcileHistoricalCompletionResponse(BaseModel):
    success: bool
    reconciled: bool
    robot: str
    task_id: str
    task_status: str
    fleet_state_task_id: str


class ReconcileHistoricalCancellationRequest(BaseModel):
    fleet: str = Field(min_length=1)
    robot: str = Field(min_length=1)
    expected_task_id: str = Field(min_length=1)
    reason: Literal["operator_confirmed_historical_abandonment"]


class ReconcileHistoricalCancellationResponse(BaseModel):
    success: bool
    reconciled: bool
    robot: str
    task_id: str
    task_status: str
    fleet_state_task_id: str


async def cancellation_lots_from_building_map(
    logger: LoggerAdapter, rmf_repo: RmfRepository
) -> list[str]:
    building_map = None
    try:
        building_map = await get_building_map(rmf_repo)
    except HTTPException:
        logger.warning(
            "No building map found, cannot obtain emergency lots for cancellation behavior"
        )
        return []

    cancellation_lots = []
    for level in building_map.levels:
        for graph in level.nav_graphs:
            for node in graph.vertices:
                # FIXME: Use properties to determine if a node is a cancellation
                # behavior lot, instead of substring matching.
                if "emergency" in node.name.lower():
                    cancellation_lots.append(node.name)
    logger.debug(f"Cancellation lots found: {cancellation_lots}")
    return cancellation_lots


@router.get("/{task_id}/request", response_model=mdl.TaskRequest)
async def get_task_request(
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    task_id: str = Path(..., description="task_id"),
):
    result = await task_repo.get_task_request(task_id)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.get("/requests", response_model=list[mdl.TaskRequest])
async def query_task_requests(
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    task_ids: str | None = Query(None, description="comma separated list of task ids"),
):
    task_id_splits = []
    if task_ids is not None:
        task_id_splits = task_ids.split(",")
    return await task_repo.query_task_requests(task_id_splits)


@router.get("", response_model=list[mdl.TaskState])
async def query_task_states(
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    task_id: Annotated[
        str | None, Query(description="comma separated list of task ids")
    ] = None,
    category: Annotated[
        str | None, Query(description="comma separated list of task categories")
    ] = None,
    request_time_between: Annotated[
        tuple[datetime, datetime] | None,
        Depends(time_between_query("request_time_between")),
    ] = None,
    requester: Annotated[
        str | None, Query(description="comma separated list of requester names")
    ] = None,
    assigned_to: Annotated[
        str | None,
        Query(description="comma separated list of assigned robot names"),
    ] = None,
    start_time_between: Annotated[
        tuple[datetime, datetime] | None,
        Depends(time_between_query("start_time_between")),
    ] = None,
    finish_time_between: Annotated[
        tuple[datetime, datetime] | None,
        Depends(time_between_query("finish_time_between")),
    ] = None,
    status: Annotated[
        str | None, Query(description="comma separated list of statuses")
    ] = None,
    label: Annotated[
        str | None,
        Query(
            description="comma separated list of labels, each item must be in the form <key>=<value>, multiple items will filter tasks with all the labels",
        ),
    ] = None,
    pagination: Annotated[mdl.Pagination | None, Depends(pagination_query)] = None,
):
    labels = mdl.Labels.from_strings(label.split(",")) if label else mdl.Labels(root={})

    return await task_repo.query_task_states(
        task_id=task_id.split(",") if task_id else None,
        category=category.split(",") if category else None,
        assigned_to=assigned_to.split(",") if assigned_to else None,
        start_time_between=start_time_between,
        finish_time_between=finish_time_between,
        request_time_between=request_time_between,
        requester=requester.split(",") if requester else None,
        status=status.split(",") if status else None,
        label=labels,
        pagination=pagination,
    )


@router.get("/{task_id}/state", response_model=mdl.TaskState)
async def get_task_state(
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    task_id: Annotated[str, Path(..., description="task_id")],
):
    """
    Available in socket.io
    """
    result = await task_repo.get_task_state(task_id)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.sub("/{task_id}/state", response_model=mdl.TaskState)
async def sub_task_state(req: SubscriptionRequest, task_id: str):
    user = req.user
    task_repo = TaskRepository(user, default_logger)
    obs = get_task_events().task_states.pipe(
        rxops.filter(lambda x: x.booking.id == task_id)
    )
    current_state = await get_task_state(task_repo, task_id)
    if current_state:
        return obs.pipe(rxops.start_with(current_state))
    return obs


@router.get("/{task_id}/booking_label", response_model=mdl.Labels)
async def get_task_booking_label(
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    task_id: Annotated[str, Path(..., description="task_id")],
):
    state = await task_repo.get_task_state(task_id)
    if state is None:
        raise HTTPException(status_code=404)

    if state.booking.labels is not None:
        for label in state.booking.labels:
            if len(label) == 0:
                continue

            booking_label = mdl.Labels.from_strings(label)
            if booking_label is not None:
                return booking_label
    raise HTTPException(status_code=404)


@router.get("/{task_id}/log", response_model=mdl.TaskEventLog)
async def get_task_log(
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    task_id: Annotated[str, Path(..., description="task_id")],
    between: Annotated[
        tuple[datetime, datetime] | None,
        Depends(time_between_query("between", default="-60000")),
    ],
):
    """
    Available in socket.io
    """

    result = await task_repo.get_task_log(task_id, between)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@router.sub("/{task_id}/log", response_model=mdl.TaskEventLog)
async def sub_task_log(_req: SubscriptionRequest, task_id: str):
    return get_task_events().task_event_logs.pipe(
        rxops.filter(lambda x: x.task_id == task_id)
    )


@router.post("/cancel_task", response_model=mdl.TaskCancelResponse)
async def post_cancel_task(
    request: Annotated[mdl.CancelTaskRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    logger: Annotated[LoggerAdapter, Depends(get_logger)],
):
    task_id = request.task_id
    logger.info("Requesting RMF task cancellation task_id=%s", task_id)

    try:
        response = await tasks_service.call(
            request.model_dump_json(exclude_none=True)
        )
        logger.info("RMF task cancellation acknowledged task_id=%s", task_id)
        return RawJSONResponse(response)
    except HTTPException as error:
        if error.status_code != 500 or error.detail != "rmf service timed out":
            raise

        # A cancel request is broadcast to the robot TaskManagers. Once a task
        # has already reached a terminal state, no TaskManager owns it anymore,
        # so RMF does not publish an ApiResponse. Treat that race as an
        # idempotent cancellation instead of reporting an internal server error.
        state = await task_repo.get_task_state(task_id)
        terminal_statuses = {
            mdl.TaskStatus.canceled,
            mdl.TaskStatus.killed,
            mdl.TaskStatus.completed,
            mdl.TaskStatus.failed,
        }
        if state is None or state.status in terminal_statuses:
            status = state.status.value if state and state.status else "not_found"
            logger.warning(
                "RMF cancellation timed out after task was already absent or terminal "
                "task_id=%s status=%s; returning idempotent success",
                task_id,
                status,
            )
            return RawJSONResponse('{"success":true}')

        # A queued/standby task has not been adopted by a robot TaskManager,
        # so the ApiRequest broadcast has no owner that can acknowledge it.
        # Cancel it through the dispatcher service instead of leaving a
        # permanent task that may execute unexpectedly after reconnect.
        if state.status in {mdl.TaskStatus.queued, mdl.TaskStatus.standby}:
            try:
                if await get_rmf_gateway().cancel_queued_task(task_id):
                    logger.warning(
                        "RMF queued cancellation completed through dispatcher "
                        "service task_id=%s status=%s",
                        task_id,
                        state.status.value,
                    )
                    return RawJSONResponse('{"success":true}')
            except HTTPException as fallback_error:
                logger.error(
                    "RMF dispatcher cancellation failed task_id=%s error=%s",
                    task_id,
                    fallback_error.detail,
                )

        status = state.status.value if state and state.status else "not_found"
        logger.error(
            "RMF cancellation was not acknowledged task_id=%s status=%s",
            task_id,
            status,
        )
        raise HTTPException(
            status_code=504,
            detail={
                "message": "RMF did not acknowledge the cancellation request",
                "task_id": task_id,
                "task_status": status,
            },
        ) from error


@router.post(
    "/reconcile_robot_reset",
    response_model=ReconcileRobotResetResponse,
)
async def post_reconcile_robot_reset(
    request: Annotated[ReconcileRobotResetRequest, Body(...)],
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    fleet_repo: Annotated[FleetRepository, Depends(FleetRepository)],
    logger: Annotated[LoggerAdapter, Depends(get_logger)],
):
    """Administratively close one orphan after a confirmed dock reset.

    This is intentionally a compare-and-set operation, not a general task
    state editor. The operator must name the exact old UUID and the latest
    fleet snapshot must prove that the robot has not adopted that UUID (or a
    replacement task) after being physically reset at its home dock.
    """
    state = await task_repo.get_task_state(request.expected_task_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "task not found", "task_id": request.expected_task_id},
        )

    assigned_robot = state.assigned_to.name if state.assigned_to else ""
    assigned_fleet = state.assigned_to.group if state.assigned_to else ""
    if assigned_robot != request.robot or assigned_fleet != request.fleet:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "task assignment does not match reset target",
                "task_id": request.expected_task_id,
                "assigned_fleet": assigned_fleet,
                "assigned_robot": assigned_robot,
            },
        )

    terminal = {
        mdl.TaskStatus.canceled,
        mdl.TaskStatus.killed,
        mdl.TaskStatus.completed,
        mdl.TaskStatus.failed,
        mdl.TaskStatus.skipped,
    }
    if state.status in terminal:
        return ReconcileRobotResetResponse(
            success=True,
            reconciled=False,
            robot=request.robot,
            task_id=request.expected_task_id,
            task_status=state.status.value,
            fleet_state_task_id="",
        )
    reconcilable = {
        mdl.TaskStatus.queued,
        mdl.TaskStatus.standby,
        mdl.TaskStatus.underway,
    }
    if state.status not in reconcilable:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "only a queued, standby, or underway orphan can be reconciled",
                "task_id": request.expected_task_id,
                "task_status": state.status.value if state.status else "unknown",
            },
        )

    fleet_state = await fleet_repo.get_fleet_state(request.fleet)
    robot_state = (fleet_state.robots or {}).get(request.robot) if fleet_state else None
    if robot_state is None or robot_state.unix_millis_time is None:
        raise HTTPException(
            status_code=409,
            detail={"message": "no timestamped fleet state for reset target"},
        )
    age_millis = int(time.time() * 1000) - robot_state.unix_millis_time
    if age_millis < 0 or age_millis > 30_000:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "fleet state is not fresh",
                "fleet_state_age_millis": age_millis,
            },
        )

    observed_task_id = str(robot_state.task_id or "")
    if observed_task_id:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "robot still reports an active task",
                "expected_task_id": request.expected_task_id,
                "fleet_state_task_id": observed_task_id,
            },
        )

    now_millis = int(time.time() * 1000)
    state.status = mdl.TaskStatus.canceled
    state.unix_millis_finish_time = now_millis
    state.cancellation = Cancellation(
        unix_millis_request_time=now_millis,
        labels=[request.reason, "source=api_server_manual_reconciliation"],
    )
    state.active = None
    state.pending = []
    reconciled = await task_repo.compare_and_set_underway_task_terminal(
        state, request.robot
    )
    if not reconciled:
        latest = await task_repo.get_task_state(request.expected_task_id)
        if latest is not None and latest.status in terminal:
            return ReconcileRobotResetResponse(
                success=True,
                reconciled=False,
                robot=request.robot,
                task_id=request.expected_task_id,
                task_status=latest.status.value,
                fleet_state_task_id=observed_task_id,
            )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "task changed while reset reconciliation was in progress",
                "task_id": request.expected_task_id,
            },
        )
    get_task_events().task_states.on_next(state)
    logger.warning(
        "Operator-confirmed dock reset reconciled orphan task task_id=%s "
        "fleet=%s robot=%s",
        request.expected_task_id,
        request.fleet,
        request.robot,
    )
    return ReconcileRobotResetResponse(
        success=True,
        reconciled=True,
        robot=request.robot,
        task_id=request.expected_task_id,
        task_status=state.status.value,
        fleet_state_task_id=observed_task_id,
    )


@router.post(
    "/reconcile_historical_completion",
    response_model=ReconcileHistoricalCompletionResponse,
)
async def post_reconcile_historical_completion(
    request: Annotated[ReconcileHistoricalCompletionRequest, Body(...)],
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    fleet_repo: Annotated[FleetRepository, Depends(FleetRepository)],
    logger: Annotated[LoggerAdapter, Depends(get_logger)],
):
    """Normalize one proven finished historical task without deleting history.

    RMF can persist a top-level ``standby`` projection even though the active
    final phase has a finish timestamp and is listed in ``completed``. This
    exact compare-and-set path is deliberately narrower than cancellation: it
    accepts only that contradictory shape, requires a fresh idle robot
    snapshot, and records the truthful terminal state as ``completed``.
    """
    state = await task_repo.get_task_state(request.expected_task_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "task not found", "task_id": request.expected_task_id},
        )

    assigned_robot = state.assigned_to.name if state.assigned_to else ""
    assigned_fleet = state.assigned_to.group if state.assigned_to else ""
    if assigned_robot != request.robot or assigned_fleet != request.fleet:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "task assignment does not match historical target",
                "task_id": request.expected_task_id,
                "assigned_fleet": assigned_fleet,
                "assigned_robot": assigned_robot,
            },
        )

    if state.cancellation is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "task contains cancellation evidence",
                "task_id": request.expected_task_id,
            },
        )

    if state.status == mdl.TaskStatus.completed:
        return ReconcileHistoricalCompletionResponse(
            success=True,
            reconciled=False,
            robot=request.robot,
            task_id=request.expected_task_id,
            task_status=state.status.value,
            fleet_state_task_id="",
        )
    if state.status != mdl.TaskStatus.standby:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "only a contradictory standby task can be normalized",
                "task_id": request.expected_task_id,
                "task_status": state.status.value if state.status else "unknown",
            },
        )

    active_id = getattr(state.active, "root", state.active)
    active_phase = (
        state.phases.get(str(active_id))
        if state.active is not None and state.phases
        else None
    )
    completion_proven = (
        state.active is not None
        and state.completed is not None
        and state.active in state.completed
        and state.pending == []
        and active_phase is not None
        and bool(active_phase.unix_millis_finish_time)
    )
    if not completion_proven:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "task does not prove final-phase completion",
                "task_id": request.expected_task_id,
            },
        )

    now_millis = int(time.time() * 1000)
    finish_millis = int(active_phase.unix_millis_finish_time or 0)
    age_millis = now_millis - finish_millis
    if age_millis < 3_600_000:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "finished task is not old enough for historical reconciliation",
                "task_id": request.expected_task_id,
                "finish_age_millis": age_millis,
            },
        )

    fleet_state = await fleet_repo.get_fleet_state(request.fleet)
    robot_state = (fleet_state.robots or {}).get(request.robot) if fleet_state else None
    if robot_state is None or robot_state.unix_millis_time is None:
        raise HTTPException(
            status_code=409,
            detail={"message": "no timestamped fleet state for historical target"},
        )
    fleet_age_millis = now_millis - robot_state.unix_millis_time
    if fleet_age_millis < 0 or fleet_age_millis > 30_000:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "fleet state is not fresh",
                "fleet_state_age_millis": fleet_age_millis,
            },
        )
    observed_task_id = str(robot_state.task_id or "")
    if observed_task_id:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "robot still reports an active task",
                "expected_task_id": request.expected_task_id,
                "fleet_state_task_id": observed_task_id,
            },
        )

    state.status = mdl.TaskStatus.completed
    state.unix_millis_finish_time = finish_millis
    state.active = None
    state.pending = []
    reconciled = await task_repo.compare_and_set_standby_task_completed(
        state, request.robot
    )
    if not reconciled:
        latest = await task_repo.get_task_state(request.expected_task_id)
        if latest is not None and latest.status == mdl.TaskStatus.completed:
            return ReconcileHistoricalCompletionResponse(
                success=True,
                reconciled=False,
                robot=request.robot,
                task_id=request.expected_task_id,
                task_status=latest.status.value,
                fleet_state_task_id=observed_task_id,
            )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "task changed during historical reconciliation",
                "task_id": request.expected_task_id,
            },
        )
    get_task_events().task_states.on_next(state)
    logger.warning(
        "Historical final-phase contradiction normalized task_id=%s fleet=%s "
        "robot=%s reason=%s",
        request.expected_task_id,
        request.fleet,
        request.robot,
        request.reason,
    )
    return ReconcileHistoricalCompletionResponse(
        success=True,
        reconciled=True,
        robot=request.robot,
        task_id=request.expected_task_id,
        task_status=state.status.value,
        fleet_state_task_id=observed_task_id,
    )


@router.post(
    "/reconcile_historical_cancellation",
    response_model=ReconcileHistoricalCancellationResponse,
)
async def post_reconcile_historical_cancellation(
    request: Annotated[ReconcileHistoricalCancellationRequest, Body(...)],
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    fleet_repo: Annotated[FleetRepository, Depends(FleetRepository)],
    logger: Annotated[LoggerAdapter, Depends(get_logger)],
):
    """Administratively cancel one old ownerless task lacking completion proof."""
    state = await task_repo.get_task_state(request.expected_task_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "task not found", "task_id": request.expected_task_id},
        )
    assigned_robot = state.assigned_to.name if state.assigned_to else ""
    assigned_fleet = state.assigned_to.group if state.assigned_to else ""
    if assigned_robot != request.robot or assigned_fleet != request.fleet:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "task assignment does not match historical target",
                "assigned_fleet": assigned_fleet,
                "assigned_robot": assigned_robot,
            },
        )
    if state.status == mdl.TaskStatus.canceled:
        return ReconcileHistoricalCancellationResponse(
            success=True, reconciled=False, robot=request.robot,
            task_id=request.expected_task_id, task_status=state.status.value,
            fleet_state_task_id="",
        )
    if state.status != mdl.TaskStatus.standby:
        raise HTTPException(
            status_code=409,
            detail={"message": "only a standby orphan can be canceled",
                    "task_status": state.status.value if state.status else "unknown"},
        )

    active_id = getattr(state.active, "root", state.active)
    active_phase = (
        state.phases.get(str(active_id))
        if active_id is not None and state.phases else None
    )
    completion_proven = (
        state.active is not None
        and state.completed is not None
        and state.active in state.completed
        and state.pending == []
        and active_phase is not None
        and bool(active_phase.unix_millis_finish_time)
    )
    if completion_proven:
        raise HTTPException(
            status_code=409,
            detail={"message": "task has completion proof; use completion reconciliation"},
        )

    now_millis = int(time.time() * 1000)
    age_anchor = (
        state.unix_millis_start_time
        or state.booking.unix_millis_request_time
        or 0
    )
    age_millis = now_millis - int(age_anchor)
    if not age_anchor or age_millis < 3_600_000:
        raise HTTPException(
            status_code=409,
            detail={"message": "task is not old enough for historical cancellation",
                    "task_age_millis": age_millis},
        )

    fleet_state = await fleet_repo.get_fleet_state(request.fleet)
    robot_state = (fleet_state.robots or {}).get(request.robot) if fleet_state else None
    if robot_state is None or robot_state.unix_millis_time is None:
        raise HTTPException(409, detail={"message": "no timestamped fleet state"})
    fleet_age_millis = now_millis - robot_state.unix_millis_time
    if fleet_age_millis < 0 or fleet_age_millis > 30_000:
        raise HTTPException(
            409, detail={"message": "fleet state is not fresh",
                         "fleet_state_age_millis": fleet_age_millis})
    observed_task_id = str(robot_state.task_id or "")
    if observed_task_id:
        raise HTTPException(
            409, detail={"message": "robot still reports an active task",
                         "fleet_state_task_id": observed_task_id})

    state.status = mdl.TaskStatus.canceled
    state.unix_millis_finish_time = now_millis
    state.cancellation = Cancellation(
        unix_millis_request_time=now_millis,
        labels=[request.reason, "source=api_server_historical_reconciliation"],
    )
    state.active = None
    state.pending = []
    reconciled = await task_repo.compare_and_set_underway_task_terminal(
        state, request.robot
    )
    if not reconciled:
        latest = await task_repo.get_task_state(request.expected_task_id)
        if latest is not None and latest.status == mdl.TaskStatus.canceled:
            return ReconcileHistoricalCancellationResponse(
                success=True, reconciled=False, robot=request.robot,
                task_id=request.expected_task_id, task_status=latest.status.value,
                fleet_state_task_id=observed_task_id,
            )
        raise HTTPException(
            409, detail={"message": "task changed during historical cancellation"})
    get_task_events().task_states.on_next(state)
    logger.warning(
        "Historical ownerless task canceled task_id=%s fleet=%s robot=%s reason=%s",
        request.expected_task_id, request.fleet, request.robot, request.reason,
    )
    return ReconcileHistoricalCancellationResponse(
        success=True, reconciled=True, robot=request.robot,
        task_id=request.expected_task_id, task_status=state.status.value,
        fleet_state_task_id=observed_task_id,
    )


def is_task_dispatch_response_success(
    root: mdl.TaskDispatchResponse1 | mdl.TaskDispatchResponse2,
) -> TypeGuard[mdl.TaskDispatchResponse1]:
    return bool(root.success)


@router.post(
    "/dispatch_task",
    response_model=mdl.TaskDispatchResponse,
    responses={400: {"model": mdl.TaskDispatchResponse}},
)
async def post_dispatch_task(
    request: Annotated[mdl.DispatchTaskRequest, Body(...)],
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    logger: Annotated[LoggerAdapter, Depends(get_logger)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    logger.debug("dispatching task to rmf %s", request)
    resp = mdl.TaskDispatchResponse.model_validate_json(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )
    logger.debug("rmf response %s", resp)
    if not is_task_dispatch_response_success(resp.root):
        return RawJSONResponse(resp.model_dump_json(), 400)
    await task_repo.save_task_state(resp.root.state)
    await task_repo.save_task_request(resp.root.state, request.request)
    return resp


@router.post(
    "/robot_task",
    response_model=mdl.RobotTaskResponse,
    responses={400: {"model": mdl.RobotTaskResponse}},
)
async def post_robot_task(
    request: Annotated[mdl.RobotTaskRequest, Body(...)],
    task_repo: Annotated[TaskRepository, Depends(TaskRepository)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    resp = mdl.RobotTaskResponse.model_validate_json(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )
    if not is_task_dispatch_response_success(resp.root.root):
        return RawJSONResponse(resp.model_dump_json(), 400)
    await task_repo.save_task_state(resp.root.root.state)
    return resp


@router.post("/interrupt_task", response_model=mdl.TaskInterruptionResponse)
async def post_interrupt_task(
    request: Annotated[mdl.TaskInterruptionRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    return RawJSONResponse(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )


@router.post("/kill_task", response_model=mdl.TaskKillResponse)
async def post_kill_task(
    request: Annotated[mdl.TaskKillRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    return RawJSONResponse(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )


@router.post("/resume_task", response_model=mdl.TaskResumeResponse)
async def post_resume_task(
    request: Annotated[mdl.TaskResumeRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    return RawJSONResponse(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )


@router.post("/rewind_task", response_model=mdl.TaskRewindResponse)
async def post_rewind_task(
    request: Annotated[mdl.TaskRewindRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    return RawJSONResponse(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )


@router.post("/skip_phase", response_model=mdl.SkipPhaseResponse)
async def post_skip_phase(
    request: Annotated[mdl.TaskPhaseSkipRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    return RawJSONResponse(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )


@router.post("/task_discovery", response_model=mdl.TaskDiscovery)
async def post_task_discovery(
    request: Annotated[mdl.TaskDiscoveryRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    return RawJSONResponse(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )


@router.post("/undo_skip_phase", response_model=mdl.UndoPhaseSkipResponse)
async def post_undo_skip_phase(
    request: Annotated[mdl.UndoPhaseSkipRequest, Body(...)],
    tasks_service: Annotated[RmfService, Depends(get_tasks_service)],
):
    return RawJSONResponse(
        await tasks_service.call(request.model_dump_json(exclude_none=True))
    )
