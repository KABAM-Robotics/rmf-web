from datetime import datetime, timezone
from typing import Annotated, Sequence

import tortoise.functions as tfuncs
from api_server.authenticator import user_dep
from api_server.logging import LoggerAdapter, get_logger
from api_server.models import (
    Labels,
    LogEntry,
    Pagination,
    Phases,
    TaskEventLog,
    TaskRequest,
    TaskState,
    TaskStatus,
    User,
)
from api_server.models import tortoise_models as ttm
from api_server.models.tortoise_models import TaskRequest as DbTaskRequest
from api_server.models.tortoise_models import TaskState as DbTaskState
from fastapi import Depends, HTTPException
from tortoise.exceptions import FieldError, IntegrityError
from tortoise.expressions import Expression, Q
from tortoise.query_utils import Prefetch
from tortoise.transactions import in_transaction


def normalize_contradictory_terminal_state(task_state: TaskState) -> bool:
    """Normalize RMF's narrow completed-final-phase/standby contradiction.

    A lift-arrival replan can leave superseded legacy events underway while the
    task's final phase is already listed as completed with a finish timestamp.
    Do not infer completion from a finish estimate, stale event, or a task that
    carries explicit cancellation evidence.
    """
    if (
        task_state.cancellation is not None
        or task_state.active is None
        or task_state.completed is None
        or task_state.active not in task_state.completed
        or task_state.pending is None
        or task_state.pending
        or not task_state.phases
    ):
        return False
    active_id = getattr(task_state.active, "root", task_state.active)
    phase = task_state.phases.get(str(active_id))
    if phase is None or not phase.unix_millis_finish_time:
        return False
    if task_state.status == TaskStatus.completed:
        return False
    task_state.status = TaskStatus.completed
    return True


class TaskRepository:
    def __init__(
        self,
        user: Annotated[User, Depends(user_dep)],
        logger: Annotated[LoggerAdapter, Depends(get_logger)],
    ):
        self.user = user
        self.logger = logger

    async def save_task_request(
        self, task_state: TaskState, task_request: TaskRequest
    ) -> None:
        await DbTaskRequest.update_or_create(
            {"request": task_request.model_dump_json()}, id_=task_state.booking.id
        )

    async def get_task_request(self, task_id: str) -> TaskRequest | None:
        result = await DbTaskRequest.get_or_none(id_=task_id)
        if result is None:
            return None
        return TaskRequest.model_validate(result.request)

    async def query_task_requests(self, task_ids: list[str]) -> list[DbTaskRequest]:
        filters = {"id___in": task_ids}
        try:
            return await DbTaskRequest.filter(**filters)
        except FieldError as e:
            raise HTTPException(422, str(e)) from e

    async def save_task_labels(
        self, db_task_state: ttm.TaskState, labels: Labels
    ) -> None:
        for k, v in labels.root.items():
            await ttm.TaskLabel.update_or_create(
                {"label_value": v},
                state=db_task_state,
                label_name=k,
            )

    async def save_task_state(self, task_state: TaskState) -> None:
        if normalize_contradictory_terminal_state(task_state):
            self.logger.warning(
                "Normalized contradictory RMF final-phase state task_id=%s active=%s",
                task_state.booking.id,
                task_state.active,
            )
        async with in_transaction():
            # RENYU fix for labels, requester and request time disappearing. Either something in rmf core or fleet adapter is removing them from tasks
            # The original dispatch task has correct details so I'm just passing in the old values to future updates of tasks
            # Get old task state and old values
            old_task_state = await self.get_task_state(task_state.booking.id)
            try:
                old_labels = old_task_state.booking.labels
            except Exception as e:
                old_labels = None
            try:
                old_requester = old_task_state.booking.requester
            except Exception as e:
                old_requester = None
            try:
                old_request_time = old_task_state.booking.unix_millis_request_time
            except Exception as e:
                old_request_time = None
            # Replace booking labels with old labels, requester and request time
            if old_labels:
                task_state.booking.labels = old_labels
            if old_requester:
                task_state.booking.requester = old_requester
            if old_request_time:
                task_state.booking.unix_millis_request_time = old_request_time
            db_task_state, created = await DbTaskState.update_or_create(
                {
                    "data": task_state.model_dump_json(),
                    "category": (
                        task_state.category.root if task_state.category else None
                    ),
                    "assigned_to": (
                        task_state.assigned_to.name if task_state.assigned_to else None
                    ),
                    "unix_millis_start_time": task_state.unix_millis_start_time
                    and datetime.fromtimestamp(
                        task_state.unix_millis_start_time / 1000, timezone.utc
                    ),
                    "unix_millis_finish_time": task_state.unix_millis_finish_time
                    and datetime.fromtimestamp(
                        task_state.unix_millis_finish_time / 1000, timezone.utc
                    ),
                    "status": task_state.status if task_state.status else None,
                    "unix_millis_request_time": task_state.booking.unix_millis_request_time
                    and datetime.fromtimestamp(
                        task_state.booking.unix_millis_request_time / 1000,
                        timezone.utc,
                    ),
                    "requester": (
                        task_state.booking.requester
                        if task_state.booking.requester
                        else None
                    ),
                },
                id_=task_state.booking.id,
            )

            # Labels attached to a task is not expected to change in task state updates,
            # so we can skip saving labels if it is not new. Note that if the labels were
            # to change, the labels we stored for querying would become out of sync.
            if created and task_state.booking.labels:
                labels = Labels.from_strings(task_state.booking.labels)
                await self.save_task_labels(db_task_state, labels)

    async def compare_and_set_underway_task_terminal(
        self,
        task_state: TaskState,
        expected_robot: str,
    ) -> bool:
        """Persist an administrative terminal projection without races.

        This deliberately updates only a non-terminal row that is still
        assigned to the operator-confirmed robot. Queued/standby rows are
        included because an API-server restart can leave a database-only task
        that the dispatcher no longer owns. A concurrent normal RMF terminal
        update wins instead of being overwritten by reconciliation.
        """
        finish_time = (
            datetime.fromtimestamp(
                task_state.unix_millis_finish_time / 1000, timezone.utc
            )
            if task_state.unix_millis_finish_time
            else None
        )
        updated = await DbTaskState.filter(
            id_=task_state.booking.id,
            status__in=[
                TaskStatus.queued,
                TaskStatus.standby,
                TaskStatus.underway,
            ],
            assigned_to=expected_robot,
        ).update(
            data=task_state.model_dump_json(),
            status=task_state.status,
            unix_millis_finish_time=finish_time,
        )
        return updated == 1

    async def compare_and_set_standby_task_completed(
        self,
        task_state: TaskState,
        expected_robot: str,
    ) -> bool:
        """Normalize one persisted standby/final-phase contradiction safely."""
        finish_time = (
            datetime.fromtimestamp(
                task_state.unix_millis_finish_time / 1000, timezone.utc
            )
            if task_state.unix_millis_finish_time
            else None
        )
        updated = await DbTaskState.filter(
            id_=task_state.booking.id,
            status=TaskStatus.standby,
            assigned_to=expected_robot,
        ).update(
            data=task_state.model_dump_json(),
            status=TaskStatus.completed,
            unix_millis_finish_time=finish_time,
        )
        return updated == 1

    async def query_task_states(
        self,
        task_id: Sequence[str] | None = None,
        category: Sequence[str] | None = None,
        assigned_to: Sequence[str] | None = None,
        start_time_between: tuple[datetime, datetime] | None = None,
        finish_time_between: tuple[datetime, datetime] | None = None,
        request_time_between: tuple[datetime, datetime] | None = None,
        requester: Sequence[str] | None = None,
        status: Sequence[str] | None = None,
        label: Labels | None = None,
        pagination: Pagination | None = None,
    ) -> list[TaskState]:
        filters = {}
        if task_id is not None:
            filters["id___in"] = task_id
        if category is not None:
            filters["category__in"] = category
        if assigned_to is not None:
            filters["assigned_to__in"] = assigned_to
        if start_time_between is not None:
            filters["unix_millis_start_time__gte"] = start_time_between[0]
            filters["unix_millis_start_time__lte"] = start_time_between[1]
        if finish_time_between is not None:
            filters["unix_millis_finish_time__gte"] = finish_time_between[0]
            filters["unix_millis_finish_time__lte"] = finish_time_between[1]
        if request_time_between is not None:
            filters["unix_millis_request_time__gte"] = request_time_between[0]
            filters["unix_millis_request_time__lte"] = request_time_between[1]
        if requester is not None:
            filters["requester__in"] = requester
        if status is not None:
            valid_values = [member.value for member in TaskStatus]
            filters["status__in"] = []
            for status_string in status:
                if status_string not in valid_values:
                    continue
                filters["status__in"].append(TaskStatus(status_string))
        query = DbTaskState.filter(**filters)

        need_group_by = False
        label_filters = {}
        if label is not None:
            label_filters.update(
                {
                    f"label_filter_{k}": tfuncs.Count(
                        "id_",
                        _filter=Q(labels__label_name=k, labels__label_value=v),
                    )
                    for k, v in label.root.items()
                }
            )

        if len(label_filters) > 0:
            filter_gt = {f"{f}__gt": 0 for f in label_filters}
            query = query.annotate(**label_filters).filter(**filter_gt)
            need_group_by = True

        if pagination:
            order_fields: list[str] = []
            annotations: dict[str, Expression] = {}
            # add annotations required for sorting by labels
            for f in pagination.order_by:
                order_prefix = f[0] if f[0] == "-" else ""
                order_field = f[1:] if order_prefix == "-" else f
                if order_field.startswith("label="):
                    f = order_field[6:]
                    annotations[f"label_sort_{f}"] = tfuncs.Max(
                        "labels__label_value",
                        _filter=Q(labels__label_name=f),
                    )
                    order_field = f"label_sort_{f}"

                order_fields.append(order_prefix + order_field)

            query = (
                query.annotate(**annotations)
                .limit(pagination.limit)
                .offset(pagination.offset)
                .order_by(*order_fields)
            )
            need_group_by = True

        if need_group_by:
            query = query.group_by("id_", "labels__state_id")

        try:
            # TODO: enforce with authz
            results = await query.values_list("data")
            return [TaskState(**r[0]) for r in results]
        except FieldError as e:
            raise HTTPException(422, str(e)) from e

    async def get_task_state(self, task_id: str) -> TaskState | None:
        # TODO: enforce with authz
        result = await DbTaskState.get_or_none(id_=task_id)
        if result is None:
            return None
        return TaskState.model_validate(result.data)

    async def get_task_log(
        self, task_id: str, between: tuple[datetime, datetime] | None
    ) -> TaskEventLog | None:
        """
        :param between: The period in unix millis to fetch.
        """
        if between:
            between_filters = {
                "unix_millis_time__gte": between[0].timestamp() * 1000,
                "unix_millis_time__lte": between[1].timestamp() * 1000,
            }
        else:
            between_filters = {}
        result = await ttm.TaskEventLog.get_or_none(task_id=task_id).prefetch_related(
            Prefetch(
                "log",
                ttm.TaskEventLogLog.filter(**between_filters),
            ),
            Prefetch(
                "phases__log", ttm.TaskEventLogPhasesLog.filter(**between_filters)
            ),
            Prefetch(
                "phases__events__log",
                ttm.TaskEventLogPhasesEventsLog.filter(**between_filters),
            ),
        )
        if result is None:
            return None
        phases = {}
        for db_phase in result.phases:
            phase = Phases(log=None, events=None)
            phase.log = [LogEntry(**dict(x)) for x in db_phase.log]
            events = {}
            for db_event in db_phase.events:
                events[db_event.event] = [LogEntry(**dict(x)) for x in db_event.log]
            phase.events = events
            phases[db_phase.phase] = phase
        return TaskEventLog(
            task_id=result.task_id,
            log=[LogEntry(**dict(x)) for x in result.log],
            phases=phases,
        )

    async def _saveEventLogs(
        self,
        db_phase: ttm.TaskEventLogPhases,
        events: dict[str, list[LogEntry]],
    ):
        for event_id, logs in events.items():
            db_event = (
                await ttm.TaskEventLogPhasesEvents.get_or_create(
                    phase=db_phase, event=event_id
                )
            )[0]
            for log in logs:
                await ttm.TaskEventLogPhasesEventsLog.create(
                    event=db_event, **log.model_dump()
                )

    async def _savePhaseLogs(
        self, db_task_log: ttm.TaskEventLog, phases: dict[str, Phases]
    ):
        for phase_id, phase in phases.items():
            db_phase = (
                await ttm.TaskEventLogPhases.get_or_create(
                    task=db_task_log, phase=phase_id
                )
            )[0]
            if phase.log:
                for log in phase.log:
                    await ttm.TaskEventLogPhasesLog.create(
                        phase=db_phase,
                        **log.model_dump(),
                    )
            if phase.events:
                await self._saveEventLogs(db_phase, phase.events)

    async def _saveTaskLogs(
        self, db_task_log: ttm.TaskEventLog, logs: Sequence[LogEntry]
    ):
        for log in logs:
            await ttm.TaskEventLogLog.create(
                task=db_task_log,
                seq=log.seq,
                unix_millis_time=log.unix_millis_time,
                tier=log.tier.name,
                text=log.text,
            )

    async def save_task_log(self, task_log: TaskEventLog) -> None:
        async with in_transaction():
            db_task_log = (
                await ttm.TaskEventLog.get_or_create(task_id=task_log.task_id)
            )[0]
            try:
                if task_log.log:
                    await self._saveTaskLogs(db_task_log, task_log.log)
                if task_log.phases:
                    await self._savePhaseLogs(db_task_log, task_log.phases)
            except IntegrityError as e:
                self.logger.error(e)
