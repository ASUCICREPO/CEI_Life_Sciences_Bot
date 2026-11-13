"""Owner-scoped listing and on-demand download of generated reports."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from http_errors import (ApiError, api_handler, decode_cursor, get_logger, invalid_request,
                         json_response, not_found, page_envelope, parse_limit, user_id_from_claims)
from shared.persistence.chat_history import ChatHistoryRepository
from shared.persistence.chat_metadata import ChatMetadataRepository, SessionNotFoundError
from shared.persistence.exports import ExportRepository, valid_export_id
from shared.persistence.ownership import OwnershipCheckError, is_session_uuid, verify_session_owner

logger = get_logger(__name__)
_exports: ExportRepository | None = None
_metadata: ChatMetadataRepository | None = None
_history: ChatHistoryRepository | None = None
_PUBLIC_FIELDS = ("export_id", "session_id", "turn_id", "filename", "format", "size", "created_at", "status")


def _repository() -> ExportRepository:
    global _exports
    if _exports is None:
        _exports = ExportRepository(os.environ["CEI_EXPORT_METADATA_TABLE"], os.environ["CEI_EXPORT_BUCKET"])
    return _exports


def _require_session(user_id: str, session_id: str) -> None:
    global _metadata, _history
    if not is_session_uuid(session_id):
        raise invalid_request("The sessionId path parameter is not valid.")
    if _metadata is None:
        _metadata = ChatMetadataRepository()
    if _history is None:
        _history = ChatHistoryRepository()
    try:
        verify_session_owner(_history, user_id, session_id)
    except SessionNotFoundError:
        raise not_found("The session was not found.") from None
    except OwnershipCheckError:
        raise ApiError(500, "internal_error", "It's not you, it's us. Something went wrong") from None
    if _metadata.get(user_id, session_id) is None:
        raise not_found("The session was not found.")


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    route = (event.get("httpMethod"), event.get("resource"))
    params = event.get("pathParameters") or {}
    repo = _repository()
    if route == ("GET", "/sessions/{sessionId}/exports"):
        session_id = params.get("sessionId")
        _require_session(user_id, session_id)
        query = event.get("queryStringParameters") or {}
        cursor = decode_cursor(query.get("cursor"), {
            "user_id": lambda value: value == user_id,
            "export_id": lambda value: valid_export_id(value) and value.startswith(session_id.lower() + "_"),
        })
        page = repo.list_for_session(user_id, session_id, limit=parse_limit(query), start_key=cursor)
        records = [{key: item[key] for key in _PUBLIC_FIELDS} for item in page.items]
        return json_response(200, page_envelope(records, page.next_key), event)
    if route == ("GET", "/exports/{exportId}/download"):
        export_id = params.get("exportId")
        if not valid_export_id(export_id):
            raise invalid_request("The exportId path parameter is not valid.")
        item = repo.get(user_id, export_id)
        if not item or item.get("status") != "ready":
            raise not_found("The export was not found.")
        _require_session(user_id, item["session_id"])
        response = json_response(200, {"download_url": repo.download_url(item),
                                      "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat()}, event)
        response["headers"]["Cache-Control"] = "no-store"
        return response
    raise ApiError(404, "not_found", "The route was not found.")
