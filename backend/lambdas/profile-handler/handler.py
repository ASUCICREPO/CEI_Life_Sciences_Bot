"""POST /profile (api.md, B112): one-time founder onboarding — first/last name and date of birth.

Stored ONLY as standard Cognito user attributes (`given_name`, `family_name`, `birthdate`) on the
existing user pool via the Admin* API -- no DynamoDB table, no pool replacement. The SPA app client's
`writeAttributes` stays empty (lib/backend-stack.ts), so this Lambda is the only writer; `birthdate` is
write-once (409 `profile_exists` once it is already set).

B112 age rule: every user must be 18+. An under-18 submission still gets its attributes written (so the
record exists for an admin to review), then the same `AdminUserGlobalSignOut` + `AdminDisableUser` calls
`lambdas/account-delete/handler.py` uses -- the user cannot sign in again; only an admin can re-enable.
The actual run-start enforcement is the separate `cei_age_verified` access-token claim (pre-token-generation
trigger), checked by `http_errors.require_age_verified` elsewhere; this route itself is never gated by it.

The caller identity is only the verified `sub` claim (B12). Cognito's Admin* APIs need the pool's own
"Username", a separate identifier for this pool (same caveat as account-delete's `cognito_username`
docstring) -- resolved here with `ListUsers` filtered on the `sub` attribute, never assumed equal to `sub`.

Logs: outcomes only, never the submitted name or date of birth (PII).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any

from http_errors import (
    ApiError,
    GENERIC_INTERNAL_ERROR,
    api_handler,
    get_logger,
    invalid_request,
    json_response,
    parse_json_body,
    user_id_from_claims,
)

logger = get_logger(__name__)

MIN_AGE_YEARS = 18
MIN_BIRTH_YEAR = 1900
NAME_MAX_LENGTH = 50
# Unicode letters (str.isalpha(), locale-independent) plus space, hyphen, apostrophe, period.
_NAME_ALLOWED_PUNCTUATION = " -'."
_BIRTHDATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SUB_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_cognito_client: Any | None = None


def _cognito() -> Any:
    global _cognito_client
    if _cognito_client is None:
        import os

        import boto3

        _cognito_client = boto3.client("cognito-idp", region_name=os.environ.get("CEI_AWS_REGION", "us-east-1"))
    return _cognito_client


def _reject_unknown(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise invalid_request(f"Unknown field: {unknown[0]}.")


def _validate_name(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise invalid_request(f"The {field} field must be a string.")
    # NFC first, so a decomposed accent ("e" + U+0301) becomes one letter that isalpha() accepts.
    name = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(name) <= NAME_MAX_LENGTH:
        raise invalid_request(f"The {field} field must be 1 to {NAME_MAX_LENGTH} characters.")
    if not name[0].isalpha() or any(not (char.isalpha() or char in _NAME_ALLOWED_PUNCTUATION) for char in name):
        raise invalid_request(
            f"The {field} field may contain only letters, spaces, hyphens, apostrophes, or periods."
        )
    return name


def _validate_birthdate(value: Any) -> date:
    if not isinstance(value, str) or not _BIRTHDATE_PATTERN.match(value):
        raise invalid_request("The birthdate field must be a date in YYYY-MM-DD format.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise invalid_request("The birthdate field is not a valid calendar date.") from None
    if parsed.year < MIN_BIRTH_YEAR:
        raise invalid_request(f"The birthdate field year must be {MIN_BIRTH_YEAR} or later.")
    if parsed > datetime.now(UTC).date():
        raise invalid_request("The birthdate field must not be in the future.")
    return parsed


def _is_adult(birth: date, *, today: date) -> bool:
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return age >= MIN_AGE_YEARS


def _attribute_map(user: dict[str, Any]) -> dict[str, str]:
    return {a["Name"]: a["Value"] for a in user.get("Attributes", []) if "Name" in a}


def _find_cognito_user(sub: str) -> dict[str, Any]:
    """The pool's own user record for this verified `sub` -- never assumed to share the Username value."""
    if not _SUB_PATTERN.match(sub):
        # The authorizer only ever hands this Lambda a verified Cognito `sub`; a non-UUID value here means
        # something upstream is broken, not a caller-supplied value to politely reject.
        logger.error("profile_sub_malformed")
        raise ApiError(500, "internal_error", GENERIC_INTERNAL_ERROR)
    response = _cognito().list_users(
        UserPoolId=_user_pool_id(), Filter=f'sub = "{sub}"', Limit=1,
    )
    users = response.get("Users") or []
    if not users:
        logger.error("profile_user_not_found")
        raise ApiError(500, "internal_error", GENERIC_INTERNAL_ERROR)
    return users[0]


def _user_pool_id() -> str:
    import os

    return os.environ["USER_POOL_ID"]


def _public_profile(given_name: str, family_name: str, birthdate: date) -> dict[str, Any]:
    return {"given_name": given_name, "family_name": family_name, "birthdate": birthdate.isoformat()}


def _create_profile(event: dict[str, Any], user_id: str) -> dict[str, Any]:
    body = parse_json_body(event)
    _reject_unknown(body, {"given_name", "family_name", "birthdate"})
    missing = [field for field in ("given_name", "family_name", "birthdate") if field not in body]
    if missing:
        raise invalid_request(f"The {missing[0]} field is required.")

    given_name = _validate_name(body["given_name"], "given_name")
    family_name = _validate_name(body["family_name"], "family_name")
    birthdate = _validate_birthdate(body["birthdate"])

    user = _find_cognito_user(user_id)
    username = user["Username"]
    if _attribute_map(user).get("birthdate"):
        raise ApiError(409, "profile_exists", "Your profile has already been set.")

    client = _cognito()
    pool_id = _user_pool_id()
    client.admin_update_user_attributes(
        UserPoolId=pool_id,
        Username=username,
        UserAttributes=[
            {"Name": "given_name", "Value": given_name},
            {"Name": "family_name", "Value": family_name},
            {"Name": "birthdate", "Value": birthdate.isoformat()},
        ],
    )

    if not _is_adult(birthdate, today=datetime.now(UTC).date()):
        # ADR: global sign-out before disable | Rationale: same ordering as account-delete/handler.py --
        # a disabled user whose refresh token is still valid could otherwise use an already-issued access
        # token for the rest of its life | Alternative: disable first rejected for the same reason there
        client.admin_user_global_sign_out(UserPoolId=pool_id, Username=username)
        client.admin_disable_user(UserPoolId=pool_id, Username=username)
        logger.info("profile_age_restricted")
        raise ApiError(403, "age_restricted", "You must be at least 18 years old to use this service.")

    logger.info("profile_created")
    return json_response(200, {"status": "ok", "profile": _public_profile(given_name, family_name, birthdate)}, event)


ROUTES = {
    ("POST", "/profile"): _create_profile,
}


@api_handler(logger)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    user_id = user_id_from_claims(event)
    route = ROUTES.get((event.get("httpMethod"), event.get("resource")))
    if route is None:
        raise ApiError(404, "not_found", "The route was not found.")
    return route(event, user_id)
