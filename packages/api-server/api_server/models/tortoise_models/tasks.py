from tortoise.fields import (
    BigIntField,
    CharField,
    DatetimeField,
    ForeignKeyField,
    ForeignKeyRelation,
    JSONField,
    ReverseRelation,
    TextField,
)
from tortoise.models import Model

from .log import LogMixin


class TaskRequest(Model):
    id_ = CharField(255, pk=True, source_field="id")
    request = JSONField()


class TaskState(Model):
    id_ = CharField(255, pk=True, source_field="id")
    data = JSONField()
    category = CharField(255, null=True, index=True)
    assigned_to = CharField(255, null=True, index=True)
    unix_millis_start_time = DatetimeField(null=True, index=True)
    unix_millis_finish_time = DatetimeField(null=True, index=True)
    unix_millis_request_time = DatetimeField(null=True, index=True)
    status = CharField(255, null=True, index=True)
    unix_millis_request_time = DatetimeField(null=True, index=True)
    requester = CharField(255, null=True, index=True)
    labels: ReverseRelation["TaskLabel"]


class TaskLabel(Model):
    state = ForeignKeyField("models.TaskState", null=True, related_name="labels")
    label_name = CharField(255, null=False, index=True)
    # Booking metadata may contain a complete multi-item delivery summary.
    # A local metadata limit must not turn an already accepted RMF task into
    # an HTTP 500. SQLite's existing VARCHAR column also stores full text.
    label_value = TextField(null=True)


class TaskEventLog(Model):
    task_id = CharField(255, pk=True)
    log: ReverseRelation["TaskEventLogLog"]
    phases: ReverseRelation["TaskEventLogPhases"]


class TaskEventLogLog(Model, LogMixin):
    task: ForeignKeyRelation[TaskEventLog] = ForeignKeyField(
        "models.TaskEventLog", related_name="log"
    )

    class Meta:  # type: ignore
        unique_together = ("task", "seq")


class TaskEventLogPhases(Model):
    task: ForeignKeyRelation[TaskEventLog] = ForeignKeyField(
        "models.TaskEventLog", related_name="phases"
    )
    phase = CharField(255)
    log: ReverseRelation["TaskEventLogPhasesLog"]
    events: ReverseRelation["TaskEventLogPhasesEvents"]

    class Meta:  # type: ignore
        unique_together = ("task", "phase")


class TaskEventLogPhasesLog(Model, LogMixin):
    phase: ForeignKeyRelation[TaskEventLogPhases] = ForeignKeyField(
        "models.TaskEventLogPhases", related_name="log"
    )

    class Meta:  # type: ignore
        unique_together = ("phase", "seq")


class TaskEventLogPhasesEvents(Model):
    phase: ForeignKeyRelation[TaskEventLogPhases] = ForeignKeyField(
        "models.TaskEventLogPhases", related_name="events"
    )
    event = CharField(255)
    log: ReverseRelation["TaskEventLogPhasesEventsLog"]

    class Meta:  # type: ignore
        unique_together = ("phase", "event")


class TaskEventLogPhasesEventsLog(Model, LogMixin):
    event: ForeignKeyRelation[TaskEventLogPhasesEvents] = ForeignKeyField(
        "models.TaskEventLogPhasesEvents", related_name="log"
    )

    class Meta:  # type: ignore
        unique_together = ("event", "seq")


class TaskFavorite(Model):
    id = CharField(255, pk=True, source_field="id")
    name = CharField(255, null=False, index=True)
    unix_millis_earliest_start_time = BigIntField(null=True, index=True)
    priority = JSONField(null=True)
    category = CharField(255, null=False, index=True)
    description = JSONField()
    user = CharField(255, null=False, index=True)
    task_definition_id = CharField(255, null=True, index=True)
