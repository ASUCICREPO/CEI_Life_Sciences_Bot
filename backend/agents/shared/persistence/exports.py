"""Owner-partitioned export metadata and private object storage.

Export IDs begin with the session UUID. This permits a strongly consistent,
paginated query for one chat and a point lookup by export ID without S3 scans
or eventually consistent secondary indexes.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key

from shared.persistence.ownership import is_session_uuid
from shared.persistence.page import Page, from_dynamo

_EXPORT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_[0-9a-f]{32}$")
_FORMATS = {"pdf", "docx", "md", "csv", "latex", "txt"}


def valid_export_id(value: Any) -> bool:
    return isinstance(value, str) and _EXPORT_ID.fullmatch(value) is not None


class ExportRepository:
    def __init__(self, table_name: str, bucket: str, *, dynamodb: Any = None, s3: Any = None) -> None:
        if not table_name or not bucket:
            raise ValueError("export storage must be configured")
        self._table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)
        self._s3 = s3 or boto3.client("s3")
        self._bucket = bucket

    def save(self, *, user_id: str, session_id: str, turn_id: str, filename: str,
             format: str, data: bytes, reviewed: bool) -> dict[str, Any]:
        """Reserve metadata before writing the object, then mark the report ready.

        The caller must hold a deletion fence through this entire operation.
        This method does not verify that fence; production activation requires
        central enforcement or a validated fence proof for every caller.
        A failed or ambiguous S3 write leaves an indexed record with its object
        key so deletion and reconciliation can find any object S3 may have saved.
        """
        if reviewed is not True:
            raise ValueError("review acknowledgment is required")
        if not user_id or not is_session_uuid(session_id) or not turn_id:
            raise ValueError("owner, session and turn are required")
        if (format not in _FORMATS or not isinstance(data, bytes) or not isinstance(filename, str)
                or not 1 <= len(filename) <= 255 or "/" in filename or "\\" in filename
                or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            raise ValueError("invalid export")
        export_id = f"{session_id.lower()}_{uuid4().hex}"
        # Neither the client-provided filename nor the user ID is needed in the object key.
        object_key = f"exports/{uuid4().hex}/{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        item = {"user_id": user_id, "export_id": export_id, "session_id": session_id.lower(),
                "turn_id": turn_id, "s3_key": object_key, "filename": filename, "format": format,
                "size": len(data), "created_at": now, "status": "processing", "reviewed_at": now}
        # If this write times out, no object upload is attempted: an ambiguous
        # reservation may remain, but an unindexed report cannot be created.
        self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(user_id)")
        try:
            self._s3.put_object(Bucket=self._bucket, Key=object_key, Body=data, StorageClass="INTELLIGENT_TIERING")
        except Exception:
            # S3 might have committed despite a timeout. Keep its key indexed;
            # a cleanup worker can delete it, and a missing object is harmless.
            try:
                self._table.update_item(Key={"user_id": user_id, "export_id": export_id},
                                        UpdateExpression="SET #status = :failed",
                                        ConditionExpression="#status = :processing AND s3_key = :key",
                                        ExpressionAttributeNames={"#status": "status"},
                                        ExpressionAttributeValues={":failed": "failed", ":processing": "processing",
                                                                   ":key": object_key})
            except Exception:
                pass  # Leave the processing reservation for reconciliation.
            raise
        # If this update fails or its result is uncertain, retain the indexed
        # reservation. Never delete an object based on an ambiguous response.
        self._table.update_item(Key={"user_id": user_id, "export_id": export_id},
                                UpdateExpression="SET #status = :ready",
                                ConditionExpression="#status = :processing AND s3_key = :key",
                                ExpressionAttributeNames={"#status": "status"},
                                ExpressionAttributeValues={":ready": "ready", ":processing": "processing",
                                                           ":key": object_key})
        item["status"] = "ready"
        return item

    def get(self, user_id: str, export_id: str) -> dict[str, Any] | None:
        if not user_id or not valid_export_id(export_id):
            return None
        item = self._table.get_item(Key={"user_id": user_id, "export_id": export_id}, ConsistentRead=True).get("Item")
        return from_dynamo(item) if item else None

    def list_for_session(self, user_id: str, session_id: str, *, limit: int = 20,
                         start_key: dict[str, Any] | None = None) -> Page[dict[str, Any]]:
        if not user_id or not is_session_uuid(session_id):
            raise ValueError("invalid owner or session")
        args: dict[str, Any] = {"KeyConditionExpression": Key("user_id").eq(user_id) &
                               Key("export_id").begins_with(session_id.lower() + "_"),
                               "ConsistentRead": True, "Limit": limit}
        if start_key:
            args["ExclusiveStartKey"] = start_key
        result = self._table.query(**args)
        return Page(items=[from_dynamo(item) for item in result.get("Items", [])],
                    next_key=result.get("LastEvaluatedKey"))

    def download_bytes(self, item: dict[str, Any]) -> bytes:
        """Server-side read-back (e.g. drafting's cold-thread carry-forward), not a client download link."""
        return self._s3.get_object(Bucket=self._bucket, Key=item["s3_key"])["Body"].read()

    def download_url(self, item: dict[str, Any]) -> str:
        filename = item.get("filename")
        if not isinstance(filename, str) or not 1 <= len(filename) <= 255 or any(ord(char) < 32 or ord(char) == 127 for char in filename):
            raise ValueError("invalid export filename")
        disposition = "attachment; filename*=UTF-8''" + quote(filename, safe="")
        return self._s3.generate_presigned_url("get_object", Params={"Bucket": self._bucket, "Key": item["s3_key"],
                                                                  "ResponseContentDisposition": disposition},
                                               ExpiresIn=900)

    def delete(self, user_id: str, export_id: str) -> None:
        item = self.get(user_id, export_id)
        if item is None:
            return
        # Delete the object first so a retry can still locate it if S3 is unavailable.
        self._s3.delete_object(Bucket=self._bucket, Key=item["s3_key"])
        self._table.delete_item(Key={"user_id": user_id, "export_id": export_id})

    def delete_session(self, user_id: str, session_id: str) -> None:
        """Delete indexed reports after the caller fences and quiesces the session.

        Metadata remains until its object is deleted, allowing safe retries. This
        queries only the owner's chat prefix and never lists the S3 bucket.
        """
        cursor = None
        while True:
            page = self.list_for_session(user_id, session_id, limit=100, start_key=cursor)
            for item in page.items:
                self.delete(user_id, item["export_id"])
            cursor = page.next_key
            if not cursor:
                return
