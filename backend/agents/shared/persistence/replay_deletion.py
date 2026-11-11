"""Remove durable SSE replay partitions after a deletion fence is established.

Callers must authenticate ownership, block new writers, and quiesce active runs
before invoking this helper. It intentionally does not expose an API route.
"""
from __future__ import annotations

from typing import Any

from boto3.dynamodb.conditions import Key

from shared.persistence.ownership import is_session_uuid


def delete_session_replays(*, user_id: str, session_id: str, runs_table: Any, history: Any) -> None:
    if not isinstance(user_id, str) or not user_id or not is_session_uuid(session_id):
        raise ValueError("invalid replay owner or session")
    prefix = f"replay#{session_id}#"
    cursor = None
    while True:
        args: dict[str, Any] = {
            "KeyConditionExpression": Key("user_id").eq(user_id) & Key("record").begins_with(prefix),
            "ConsistentRead": True,
        }
        if cursor is not None:
            args["ExclusiveStartKey"] = cursor
        page = runs_table.query(**args)
        for item in page.get("Items", []):
            record = item.get("record")
            if not isinstance(record, str) or not record.startswith(prefix):
                raise ValueError("invalid replay index")
            run_id = record[len(prefix):]
            expected = f"sse#{user_id}#{session_id}#{run_id}"
            if not is_session_uuid(run_id) or item.get("history_partition") != expected:
                raise ValueError("invalid replay partition")
            # Remove the partition before the index so retries can still find it.
            history.delete_session(expected)
            runs_table.delete_item(Key={"user_id": user_id, "record": record},
                                   ConditionExpression="history_partition = :partition",
                                   ExpressionAttributeValues={":partition": expected})
        cursor = page.get("LastEvaluatedKey")
        if not cursor:
            return
