"""POST /account/delete (api.md, F19/B109): disable + delayed sweep account deletion.

Behind a flag, off by default (`accountDeletionEnabled`, backend/lib/constructs/account-deletion.ts);
this Lambda and its route only exist in the stack when that flag is true.

Order, matching B109 exactly: (1) write the account "deleting" gate first (conditional put, so a repeat
call is idempotent and returns 202 again without moving `sweep_at` later), (2) disable the Cognito user
and revoke every session, (3) schedule the delayed sweep. Any failure after step 1 is safe to retry: the
gate write is conditional, `AdminUserGlobalSignOut`/`AdminDisableUser` are themselves idempotent (an
already-disabled, already-signed-out user still returns success), and the schedule name is derived from
a hash of the user id (`shared.persistence.account_gate.schedule_name`) so a retried `CreateSchedule`
either creates the one schedule for this user or reports `ConflictException`, treated as success.

The user id is taken only from the verified `sub` claim (B12). Cognito's Admin* APIs need the pool's
own "Username", which is a separate, unverifiable-from-sub identifier for this pool (see
`shared/persistence/account_gate.py`'s `AccountDeletionGate.cognito_username` docstring); it is read
from the `cognito:username` claim on this same authenticated request and stored on the gate so the
delete-worker -- which runs 9 hours later with only `{"user_id": sub}`, no claims -- can use it too.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from http_errors import api_handler, get_logger, json_response, user_id_from_claims
from shared.persistence.account_gate import AccountGateRepository, schedule_name

logger = get_logger(__name__)

# 8-hour AgentCore run maximum (B25) plus the 1-hour access-token life (B109): by the sweep time, any
# timed-out SDK retry from an in-flight run has already finished, so no epoch-conditional write is needed.
SWEEP_DELAY = timedelta(hours=9)

_gate_repo: AccountGateRepository | None = None
_cognito_client: Any | None = None
_scheduler_client: Any | None = None


def _gate() -> AccountGateRepository:
    global _gate_repo
    if _gate_repo is None:
        _gate_repo = AccountGateRepository()
    return _gate_repo


def _cognito() -> Any:
    global _cognito_client
    if _cognito_client is None:
        import boto3

        _cognito_client = boto3.client("cognito-idp", region_name=os.environ.get("CEI_AWS_REGION", "us-east-1"))
    return _cognito_client


def _scheduler() -> Any:
    global _scheduler_client
    if _scheduler_client is None:
        import boto3

        _scheduler_client = boto3.client("scheduler", region_name=os.environ.get("CEI_AWS_REGION", "us-east-1"))
    return _scheduler_client


def _cognito_username_from_claims(event: dict[str, Any], user_id: str) -> str:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    username = claims.get("cognito:username")
    return username if isinstance(username, str) and username else user_id


def disable_and_sign_out(user_id: str, cognito_username: str) -> None:
    client = _cognito()
    user_pool_id = os.environ["USER_POOL_ID"]
    # ADR: global sign-out before disable | Rationale: a disabled user whose refresh token is still valid
    # could otherwise use an already-issued access token for the rest of its 1-hour life | Alternative:
    # disable first rejected -- would leave a live access token usable until it naturally expires
    client.admin_user_global_sign_out(UserPoolId=user_pool_id, Username=cognito_username)
    client.admin_disable_user(UserPoolId=user_pool_id, Username=cognito_username)


def schedule_sweep(user_id: str, sweep_at: datetime) -> None:
    client = _scheduler()
    name = schedule_name(user_id)
    payload = json.dumps({"user_id": user_id})
    try:
        client.create_schedule(
            Name=name,
            GroupName=os.environ["SCHEDULE_GROUP_NAME"],
            ScheduleExpression=f"at({sweep_at.strftime('%Y-%m-%dT%H:%M:%S')})",
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={
                "Arn": os.environ["DELETE_WORKER_ARN"],
                "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
                "Input": payload,
            },
        )
    except client.exceptions.ConflictException:
        # A repeat POST /account/delete: the one schedule for this user already exists (B109 idempotency).
        pass


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    cognito_username = _cognito_username_from_claims(event, user_id)
    now = datetime.now(UTC)
    gate = _gate().write_gate(
        user_id, requested_at=now, sweep_at=now + SWEEP_DELAY, cognito_username=cognito_username,
    )
    disable_and_sign_out(user_id, gate.cognito_username or cognito_username)
    schedule_sweep(user_id, gate.sweep_at)
    return json_response(202, {"status": "deleting"}, event)
