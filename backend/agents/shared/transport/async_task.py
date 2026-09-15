from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

import boto3
from pydantic import BaseModel, ConfigDict

from shared.config import config


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    status: TaskStatus
    result: dict[str, Any] | None = None
    error: str | None = None
    progress_pct: int = 0


class TaskTransport(ABC):
    @abstractmethod
    def submit(
        self,
        agent_name: str,
        payload: dict[str, Any],
        *,
        cluster: str,
        task_definition: str,
        subnets: list[str],
    ) -> str: ...

    @abstractmethod
    def poll(self, task_id: str) -> TaskResult: ...

    @abstractmethod
    def cancel(self, task_id: str) -> None: ...


class AsyncTaskClient(TaskTransport):

    def __init__(self, *, session: boto3.Session | None = None) -> None:
        effective_session = session or boto3.Session(
            region_name=config.aws_region
        )
        self._ecs = effective_session.client("ecs")
        self._dynamodb = effective_session.resource("dynamodb")
        self._job_table = self._dynamodb.Table(config.job_id_table)

    def submit(
        self,
        agent_name: str,
        payload: dict[str, Any],
        *,
        cluster: str,
        task_definition: str,
        subnets: list[str],
    ) -> str:
        raise NotImplementedError

    def poll(self, task_id: str) -> TaskResult:
        raise NotImplementedError

    def cancel(self, task_id: str) -> None:
        raise NotImplementedError
