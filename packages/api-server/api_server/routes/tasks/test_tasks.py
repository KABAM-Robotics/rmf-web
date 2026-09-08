import time
import json

from fastapi import HTTPException
from unittest.mock import patch
from uuid import uuid4

import pydantic

from api_server import models as mdl
from api_server.logging import default_logger
from api_server.models import TaskEventLog, TaskState
from api_server.models.rmf_api.task_state import AssignedTo, Cancellation, Id
from api_server.models.tortoise_models import TaskState as DbTaskState
from api_server.repositories import FleetRepository, TaskRepository
from api_server.repositories.tasks import normalize_contradictory_terminal_state
from api_server.rmf_io import get_tasks_service
from api_server.rmf_io.events import get_task_events
from api_server.test import AppFixture, make_fleet_state, make_task_log, make_task_state


async def _delete_task_state(task_id: str):
    await DbTaskState.filter(id_=task_id).delete()


async def _force_contradictory_standby(task: TaskState):
    await DbTaskState.filter(id_=task.booking.id).update(
        data=task.model_dump_json(),
        status=mdl.TaskStatus.standby,
    )


class TestTasksRoute(AppFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        task_ids = [str(uuid4()), str(uuid4())]
        cls.task_states = [
            make_task_state(
                task_id=task_ids[0],
                labels=[
                    "test_single",
                    "test_single_2=",
                    "test_kv=value",
                    "test_label_sort=zzz",
                    "test_label_sort_2=aaa",
                    "test_label_sort_3=bbb",
                ],
            ),
            make_task_state(
                task_id=task_ids[1],
                labels=["test_label_sort=aaa", "test_label_sort_3=bbb"],
            ),
        ]
        cls.task_logs = [make_task_log(task_id=f"test_{x}") for x in task_ids]
        cls.clsSetupErr: str | None = None

        portal = cls.get_portal()
        repo = TaskRepository(cls.admin_user, default_logger)
        for x in cls.task_states:
            portal.call(repo.save_task_state, x)
        for x in cls.task_logs:
            portal.call(repo.save_task_log, x)

    def setUp(self):
        super().setUp()
        self.assertIsNone(self.clsSetupErr)

    def test_get_task_state(self):
        resp = self.client.get(f"/tasks/{self.task_states[0].booking.id}/state")
        self.assertEqual(200, resp.status_code)
        task_state = TaskState.model_validate_json(resp.content)
        self.assertEqual(
            self.task_states[0].booking.id,
            task_state.booking.id,
        )
        if task_state.booking.labels is None:
            self.fail("expected label not to be None")
        labels = mdl.Labels.from_strings(task_state.booking.labels)
        self.assertEqual("", labels.root["test_single"])
        self.assertEqual("", labels.root["test_single_2"])
        self.assertEqual("value", labels.root["test_kv"])

    def test_normalizes_only_completed_final_phase_contradiction(self):
        state = make_task_state(task_id="contradictory-final")
        state.status = mdl.TaskStatus.standby
        state.active = 2
        state.completed = [1, 2]
        state.pending = []
        state.phases["2"].unix_millis_finish_time = 1234
        self.assertTrue(normalize_contradictory_terminal_state(state))
        self.assertEqual(mdl.TaskStatus.completed, state.status)

        active = make_task_state(task_id="still-active")
        active.status = mdl.TaskStatus.standby
        active.active = 2
        active.completed = [1]
        active.pending = []
        active.phases["2"].unix_millis_finish_time = 1234
        self.assertFalse(normalize_contradictory_terminal_state(active))
        self.assertEqual(mdl.TaskStatus.standby, active.status)

        canceled = make_task_state(task_id="canceled-final")
        canceled.status = mdl.TaskStatus.standby
        canceled.active = 2
        canceled.completed = [1, 2]
        canceled.pending = []
        canceled.phases["2"].unix_millis_finish_time = 1234
        canceled.cancellation = Cancellation(
            unix_millis_request_time=1200, labels=["operator"]
        )
        self.assertFalse(normalize_contradictory_terminal_state(canceled))
        self.assertEqual(mdl.TaskStatus.standby, canceled.status)

    def test_replayed_task_log_is_idempotent(self):
        """Adapters may replay a cumulative log after reconnecting."""
        task_log = make_task_log(task_id=f"replay_{uuid4()}")
        repo = TaskRepository(self.admin_user, default_logger)
        portal = self.get_portal()

        portal.call(repo.save_task_log, task_log)
        portal.call(repo.save_task_log, task_log)

        saved = portal.call(repo.get_task_log, task_log.task_id, None)
        self.assertIsNotNone(saved)
        self.assertEqual(task_log, saved)


    def test_query_task_states(self):
        resp = self.client.get(f"/tasks?task_id={self.task_states[0].booking.id}")
        self.assertEqual(200, resp.status_code)
        results = resp.json()
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0]["booking"]["id"])

    def test_query_task_states_filter_by_label(self):
        resp = self.client.get("/tasks?label=not_existing")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

        resp = self.client.get("/tasks?label=test_single")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_single=wrong_value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

        resp = self.client.get("/tasks?label=test_single_2=")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_kv=value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_kv=wrong_value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

        resp = self.client.get("/tasks?label=test_single,test_kv=value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(1, len(results))
        self.assertEqual(self.task_states[0].booking.id, results[0].booking.id)

        resp = self.client.get("/tasks?label=test_single,test_kv=wrong_value")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(0, len(results))

    def test_query_task_states_sort_by_label(self):
        resp = self.client.get("/tasks?order_by=-label=test_label_sort")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))
        for a, b in zip(self.task_states, results):
            self.assertEqual(a, b)

        resp = self.client.get("/tasks?order_by=label=test_label_sort")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))
        for a, b in zip(self.task_states[::-1], results):
            self.assertEqual(a, b)

        # test sorting by multiple labels
        resp = self.client.get(
            "/tasks?order_by=label=test_label_sort,label=test_label_sort_3"
        )
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))
        for a, b in zip(self.task_states[::-1], results):
            self.assertEqual(a, b)

        # test that tasks without the label are not filtered out
        # we don't test the result order because different db has different behavior
        # of sorting NULL.
        resp = self.client.get("/tasks?order_by=label=test_label_sort_not_existing")
        self.assertEqual(200, resp.status_code)
        results = pydantic.TypeAdapter(list[TaskState]).validate_json(resp.content)
        self.assertEqual(2, len(results))

    def test_sub_task_state(self):
        task_id = self.task_states[0].booking.id
        with self.subscribe_sio(f"/tasks/{task_id}/state") as sub:
            get_task_events().task_states.on_next(self.task_states[0])
            state = TaskState(**next(sub))
            self.assertEqual(task_id, state.booking.id)

    def test_get_task_log(self):
        resp = self.client.get(
            f"/tasks/{self.task_logs[0].task_id}/log?between=0,1636388414500"
        )
        self.assertEqual(200, resp.status_code)
        logs = mdl.TaskEventLog(**resp.json())
        self.assertEqual(self.task_logs[0].task_id, logs.task_id)

        # check task log
        if logs.log is None:
            self.assertIsNotNone(logs.log)
            return
        self.assertEqual(1, len(logs.log))
        log = logs.log[0]  # pylint: disable=unsubscriptable-object
        self.assertEqual(0, log.seq)
        self.assertEqual(mdl.Tier.info, log.tier)
        self.assertEqual(1636388410000, log.unix_millis_time)
        self.assertEqual("Beginning task", log.text)

        # check number of phases
        if logs.phases is None:
            self.assertIsNotNone(logs.phases)
            return
        self.assertEqual(2, len(logs.phases))
        self.assertIn("1", logs.phases)
        self.assertIn("2", logs.phases)

        # check correct log
        phase1 = logs.phases["1"]  # pylint: disable=unsubscriptable-object
        phase1_log = phase1.log
        if phase1_log is None:
            self.assertIsNotNone(phase1_log)
            return
        self.assertEqual(1, len(phase1_log))
        log = phase1_log[0]
        self.assertEqual(0, log.seq)
        self.assertEqual(mdl.Tier.info, log.tier)
        self.assertEqual(1636388410000, log.unix_millis_time)
        self.assertEqual("Beginning phase", log.text)

        # check number of events
        phase1_events = phase1.events
        if phase1_events is None:
            self.assertIsNotNone(phase1_events)
            return
        self.assertEqual(
            7, len(phase1_events)
        )  # check all events are returned, including those with no logs

        # check event log
        self.assertIn("1", phase1_events)
        self.assertEqual(
            3, len(phase1_events["1"])
        )  # check only logs in the period is returned
        log = phase1_events["1"][0]
        self.assertEqual(0, log.seq)
        self.assertEqual(mdl.Tier.info, log.tier)
        self.assertEqual(1636388409995, log.unix_millis_time)
        self.assertEqual(
            "Generating plan to get from [place:parking_03] to [place:kitchen]",
            log.text,
        )

        # TODO: check relative time is working, this requires the use of
        # dependencies overrides, which requires change in the server architecture.
        # Better to do this after this gets merged into main so we don't make
        # more architecture changes.

    def test_sub_task_log(self):
        task_id = self.task_logs[0].task_id
        with self.subscribe_sio(f"/tasks/{task_id}/log") as sub:
            get_task_events().task_event_logs.on_next(self.task_logs[0])
            log = TaskEventLog(**next(sub))
            self.assertEqual(task_id, log.task_id)

    def test_cancel_task(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/cancel_task",
                content=mdl.CancelTaskRequest(
                    type="cancel_task_request", task_id="task_id", labels=None
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_cancel_task_timeout_is_idempotent_for_terminal_task(self):
        original_state = self.task_states[0]
        task_id = original_state.booking.id
        state = original_state.model_copy(
            update={"status": mdl.TaskStatus.completed}
        )
        repo = TaskRepository(self.admin_user, default_logger)
        self.get_portal().call(repo.save_task_state, state)

        try:
            with patch.object(get_tasks_service(), "call") as mock:
                mock.side_effect = HTTPException(500, "rmf service timed out")
                resp = self.client.post(
                    "/tasks/cancel_task",
                    content=mdl.CancelTaskRequest(
                        type="cancel_task_request", task_id=task_id, labels=None
                    ).model_dump_json(exclude_none=True),
                )
        finally:
            self.get_portal().call(repo.save_task_state, original_state)

        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual({"success": True}, resp.json())

    def test_cancel_task_timeout_reports_non_terminal_state(self):
        original_state = self.task_states[1]
        task_id = original_state.booking.id
        state = original_state.model_copy(
            update={"status": mdl.TaskStatus.underway}
        )
        repo = TaskRepository(self.admin_user, default_logger)
        self.get_portal().call(repo.save_task_state, state)

        try:
            with patch.object(get_tasks_service(), "call") as mock:
                mock.side_effect = HTTPException(500, "rmf service timed out")
                resp = self.client.post(
                    "/tasks/cancel_task",
                    content=mdl.CancelTaskRequest(
                        type="cancel_task_request", task_id=task_id, labels=None
                    ).model_dump_json(exclude_none=True),
                )
        finally:
            self.get_portal().call(repo.save_task_state, original_state)

        self.assertEqual(504, resp.status_code, resp.content)
        self.assertEqual(task_id, resp.json()["detail"]["task_id"])
        self.assertEqual("underway", resp.json()["detail"]["task_status"])

    def test_cancel_task_timeout_is_idempotent_when_task_is_absent(self):
        task_id = str(uuid4())
        with patch.object(get_tasks_service(), "call") as mock:
            mock.side_effect = HTTPException(500, "rmf service timed out")
            resp = self.client.post(
                "/tasks/cancel_task",
                content=mdl.CancelTaskRequest(
                    type="cancel_task_request", task_id=task_id, labels=None
                ).model_dump_json(exclude_none=True),
            )

        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual({"success": True}, resp.json())

    def _save_reset_reconciliation_fixture(
        self, task_id: str, robot_task_id: str = ""
    ):
        fleet = make_fleet_state()
        fleet.name = f"fleet-{task_id}"
        robot = f"robot-{task_id}"
        fleet.robots = {
            robot: mdl.RobotState(
                name=robot,
                status=mdl.RobotStatus.idle,
                task_id=robot_task_id,
                unix_millis_time=int(time.time() * 1000),
            )
        }
        task = make_task_state(task_id=task_id)
        task.status = mdl.TaskStatus.underway
        task.assigned_to = AssignedTo(group=fleet.name, name=robot)
        portal = self.get_portal()
        portal.call(
            FleetRepository(self.admin_user, default_logger).save_fleet_state,
            fleet,
        )
        portal.call(TaskRepository(self.admin_user, default_logger).save_task_state, task)
        return fleet, robot, task

    def test_operator_reset_reconciles_exact_orphan_and_is_idempotent(self):
        fleet, robot, task = self._save_reset_reconciliation_fixture(
            "operator-reset-orphan"
        )
        payload = {
            "fleet": fleet.name,
            "robot": robot,
            "expected_task_id": task.booking.id,
            "reason": "operator_confirmed_dock_reset",
        }
        try:
            first = self.client.post("/tasks/reconcile_robot_reset", json=payload)
            self.assertEqual(200, first.status_code, first.content)
            self.assertTrue(first.json()["reconciled"])
            self.assertEqual("canceled", first.json()["task_status"])

            second = self.client.post("/tasks/reconcile_robot_reset", json=payload)
            self.assertEqual(200, second.status_code, second.content)
            self.assertFalse(second.json()["reconciled"])
        finally:
            self.get_portal().call(_delete_task_state, task.booking.id)

    def test_operator_reset_refuses_robot_with_active_task(self):
        fleet, robot, task = self._save_reset_reconciliation_fixture(
            "operator-reset-active", robot_task_id="replacement-task"
        )
        try:
            response = self.client.post(
                "/tasks/reconcile_robot_reset",
                json={
                    "fleet": fleet.name,
                    "robot": robot,
                    "expected_task_id": task.booking.id,
                    "reason": "operator_confirmed_dock_reset",
                },
            )
            self.assertEqual(409, response.status_code, response.content)
            state = self.client.get(f"/tasks/{task.booking.id}/state").json()
            self.assertEqual("underway", state["status"])
        finally:
            self.get_portal().call(_delete_task_state, task.booking.id)

    def _save_historical_completion_fixture(
        self, task_id: str, robot_task_id: str = ""
    ):
        fleet = make_fleet_state()
        fleet.name = f"fleet-{task_id}"
        robot = f"robot-{task_id}"
        fleet.robots = {
            robot: mdl.RobotState(
                name=robot,
                status=mdl.RobotStatus.idle,
                task_id=robot_task_id,
                unix_millis_time=int(time.time() * 1000),
            )
        }
        task = make_task_state(task_id=task_id)
        task.status = mdl.TaskStatus.standby
        task.assigned_to = AssignedTo(group=fleet.name, name=robot)
        task.active = Id(root=2)
        task.completed = [Id(root=1)]
        task.pending = []
        portal = self.get_portal()
        portal.call(
            FleetRepository(self.admin_user, default_logger).save_fleet_state,
            fleet,
        )
        portal.call(TaskRepository(self.admin_user, default_logger).save_task_state, task)
        task.completed = [Id(root=1), Id(root=2)]
        task.phases["2"].unix_millis_finish_time = int(time.time() * 1000) - 7_200_000
        task.unix_millis_finish_time = task.phases["2"].unix_millis_finish_time
        portal.call(_force_contradictory_standby, task)
        return fleet, robot, task

    def test_historical_completion_normalizes_exact_contradiction(self):
        fleet, robot, task = self._save_historical_completion_fixture(
            "historical-final-phase"
        )
        payload = {
            "fleet": fleet.name,
            "robot": robot,
            "expected_task_id": task.booking.id,
            "reason": "historical_final_phase_completion",
        }
        try:
            first = self.client.post(
                "/tasks/reconcile_historical_completion", json=payload
            )
            self.assertEqual(200, first.status_code, first.content)
            self.assertTrue(first.json()["reconciled"])
            self.assertEqual("completed", first.json()["task_status"])

            second = self.client.post(
                "/tasks/reconcile_historical_completion", json=payload
            )
            self.assertEqual(200, second.status_code, second.content)
            self.assertFalse(second.json()["reconciled"])

            state = self.client.get(f"/tasks/{task.booking.id}/state").json()
            self.assertEqual("completed", state["status"])
            self.assertIsNone(state["active"])
        finally:
            self.get_portal().call(_delete_task_state, task.booking.id)

    def test_historical_completion_refuses_robot_with_active_task(self):
        fleet, robot, task = self._save_historical_completion_fixture(
            "historical-active-robot", robot_task_id="replacement-task"
        )
        try:
            response = self.client.post(
                "/tasks/reconcile_historical_completion",
                json={
                    "fleet": fleet.name,
                    "robot": robot,
                    "expected_task_id": task.booking.id,
                    "reason": "historical_final_phase_completion",
                },
            )
            self.assertEqual(409, response.status_code, response.content)
            state = self.client.get(f"/tasks/{task.booking.id}/state").json()
            self.assertEqual("standby", state["status"])
        finally:
            self.get_portal().call(_delete_task_state, task.booking.id)

    def test_historical_completion_refuses_cancellation_evidence(self):
        fleet, robot, task = self._save_historical_completion_fixture(
            "historical-canceled-final"
        )
        task.cancellation = Cancellation(
            unix_millis_request_time=int(time.time() * 1000) - 7_100_000,
            labels=["kabam_task_manager"],
        )
        self.get_portal().call(_force_contradictory_standby, task)
        try:
            response = self.client.post(
                "/tasks/reconcile_historical_completion",
                json={
                    "fleet": fleet.name,
                    "robot": robot,
                    "expected_task_id": task.booking.id,
                    "reason": "historical_final_phase_completion",
                },
            )
            self.assertEqual(409, response.status_code, response.content)
            self.assertEqual(
                "task contains cancellation evidence",
                response.json()["detail"]["message"],
            )
        finally:
            self.get_portal().call(_delete_task_state, task.booking.id)

    def _save_historical_cancellation_fixture(self, task_id: str):
        fleet = make_fleet_state()
        fleet.name = f"fleet-{task_id}"
        robot = f"robot-{task_id}"
        fleet.robots = {
            robot: mdl.RobotState(
                name=robot, status=mdl.RobotStatus.idle, task_id="",
                unix_millis_time=int(time.time() * 1000),
            )
        }
        task = make_task_state(task_id=task_id)
        task.status = mdl.TaskStatus.standby
        task.assigned_to = AssignedTo(group=fleet.name, name=robot)
        task.unix_millis_start_time = int(time.time() * 1000) - 7_200_000
        task.active = Id(root=2)
        task.completed = [Id(root=1)]
        task.pending = []
        portal = self.get_portal()
        portal.call(
            FleetRepository(self.admin_user, default_logger).save_fleet_state, fleet)
        portal.call(
            TaskRepository(self.admin_user, default_logger).save_task_state, task)
        return fleet, robot, task

    def test_historical_cancellation_closes_exact_unproven_orphan(self):
        fleet, robot, task = self._save_historical_cancellation_fixture(
            "historical-unproven-orphan")
        payload = {
            "fleet": fleet.name, "robot": robot,
            "expected_task_id": task.booking.id,
            "reason": "operator_confirmed_historical_abandonment",
        }
        try:
            first = self.client.post(
                "/tasks/reconcile_historical_cancellation", json=payload)
            self.assertEqual(200, first.status_code, first.content)
            self.assertTrue(first.json()["reconciled"])
            self.assertEqual("canceled", first.json()["task_status"])
            second = self.client.post(
                "/tasks/reconcile_historical_cancellation", json=payload)
            self.assertEqual(200, second.status_code, second.content)
            self.assertFalse(second.json()["reconciled"])
        finally:
            self.get_portal().call(_delete_task_state, task.booking.id)

    def test_historical_cancellation_refuses_completion_proof(self):
        fleet, robot, task = self._save_historical_completion_fixture(
            "historical-proven-complete")
        try:
            response = self.client.post(
                "/tasks/reconcile_historical_cancellation",
                json={
                    "fleet": fleet.name, "robot": robot,
                    "expected_task_id": task.booking.id,
                    "reason": "operator_confirmed_historical_abandonment",
                },
            )
            self.assertEqual(409, response.status_code, response.content)
        finally:
            self.get_portal().call(_delete_task_state, task.booking.id)

    def test_interrupt_task(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = '{ "success": True, "token": "token" }'
            resp = self.client.post(
                "/tasks/interrupt_task",
                content=mdl.TaskInterruptionRequest(
                    type="interrupt_task_request", task_id="task_id", labels=None
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_kill_task(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/kill_task",
                content=mdl.TaskKillRequest(
                    type="kill_task_request", task_id="task_id", labels=None
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_resume_task(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/resume_task",
                content=mdl.TaskResumeRequest(
                    type=None, for_task=None, for_tokens=None, labels=None
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_rewind_task(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = '{ "success": true }'
            resp = self.client.post(
                "/tasks/rewind_task",
                content=mdl.TaskRewindRequest(
                    type="rewind_task_request", task_id="task_id", phase_id=0
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_skip_phase(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = '{ "success": True, "token": "token" }'
            resp = self.client.post(
                "/tasks/skip_phase",
                content=mdl.TaskPhaseSkipRequest(
                    type="skip_phase_request",
                    task_id="task_id",
                    phase_id=0,
                    labels=None,
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_task_discovery(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = "{}"
            resp = self.client.post(
                "/tasks/task_discovery",
                content=mdl.TaskDiscoveryRequest(
                    type="task_discovery_request"
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)

    def test_undo_skip_phase(self):
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = '{ "success": True }'
            resp = self.client.post(
                "/tasks/undo_skip_phase",
                content=mdl.UndoPhaseSkipRequest(
                    type="undo_phase_skip_request",
                ).model_dump_json(exclude_none=True),
            )
            self.assertEqual(200, resp.status_code, resp.content)


class TestDispatchTask(AppFixture):
    def test_accepted_robot_task_preserves_long_delivery_label(self):
        task_id = str(uuid4())
        summary = ", ".join(["1x Room Delivery"] * 18)
        self.assertEqual(322, len(summary))
        label = "delivery_items=" + summary
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = json.dumps({"success": True, "state": {
                "booking": {"id": task_id, "labels": [label]}}})
            response = self.client.post("/tasks/robot_task", json={
                "type": "robot_task_request", "fleet": "Keenon_W3", "robot": "Anna",
                "request": {"category": "test", "description": "test"}})
            self.assertEqual(200, response.status_code, response.content)
            self.assertEqual(task_id, response.json()["state"]["booking"]["id"])
            mock.assert_called_once()
        state = self.client.get(f"/tasks/{task_id}/state")
        self.assertEqual(200, state.status_code, state.content)
        self.assertEqual([label], state.json()["booking"]["labels"])
        async def read_stored_label():
            from api_server.models.tortoise_models import TaskLabel
            row = await TaskLabel.get(state_id=task_id, label_name="delivery_items")
            return row.label_value
        self.assertEqual(summary, self.get_portal().call(read_stored_label))

    def post_task_request(self):
        return self.client.post(
            "/tasks/dispatch_task",
            content=mdl.DispatchTaskRequest(
                type="dispatch_task_request",
                request=mdl.TaskRequest(
                    category="test",
                    description="description",
                ),
            ).model_dump_json(exclude_none=True),
        )

    def test_success(self):
        task_id = str(uuid4())
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'
            resp = self.post_task_request()
            self.assertEqual(200, resp.status_code, resp.content)

        # check that the task is already in the database by the time the dispatch request returns
        resp = self.client.get(f"/tasks/{task_id}/state")
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual(task_id, resp.json()["booking"]["id"])

    def test_task_request_exist(self):
        task_id = str(uuid4())
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = f'{{ "success": true, "state": {{ "booking": {{ "id": "{task_id}" }} }} }}'
            resp = self.post_task_request()
            self.assertEqual(200, resp.status_code, resp.content)

        # check that the task request is in the database
        resp = self.client.get(f"/tasks/{task_id}/request")
        self.assertEqual(200, resp.status_code, resp.content)
        self.assertEqual("test", resp.json()["category"])
        self.assertEqual("description", resp.json()["description"])

    def test_fail_with_multiple_errors(self):
        # fails with multiple errors
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = """{
                "success": false,
                "errors": [
                    { "code": 1, "category": "test_error_1", "detail": "detail 1" },
                    { "code": 2, "category": "test_error_2", "detail": "detail 2" }
                ]
            }
            """
            resp = self.post_task_request()
            self.assertEqual(400, resp.status_code, resp.content)

    def test_fail_with_no_errors(self):
        # fails with multiple errors
        with patch.object(get_tasks_service(), "call") as mock:
            mock.return_value = """{
                "success": false
            }
            """
            resp = self.post_task_request()
            self.assertEqual(400, resp.status_code, resp.content)
