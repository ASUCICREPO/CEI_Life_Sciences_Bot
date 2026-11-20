import copy
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ADR: load by file path under a unique name | Rationale: several Lambdas have a top-level handler.py;
# a plain `import handler` would reuse whichever one another test file cached first
_spec = importlib.util.spec_from_file_location(
    "pre_token_generation_handler", Path(__file__).resolve().parents[1] / "handler.py"
)
trigger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trigger)

ROUTER = "routerclient123"
SUB = "3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab"



TODAY = datetime.now(UTC).date()
ADULT_BIRTHDATE = TODAY.replace(year=TODAY.year - 25).isoformat()
MINOR_BIRTHDATE = TODAY.replace(year=TODAY.year - 10).isoformat()


def user_event(source="TokenGeneration_Authentication", user_attributes=None):
    return {
        "triggerSource": source,
        "callerContext": {"clientId": "spa"},
        "request": {"userAttributes": user_attributes or {}},
        "response": {"claimsAndScopeOverrideDetails": None},
    }


def m2m_event(metadata=None, client_id=ROUTER):
    return {
        "version": "3",
        "triggerSource": "TokenGeneration_ClientCredentials",
        "callerContext": {"clientId": client_id},
        "request": {"scopes": ["cei-tools/src.pubmed"], "clientMetadata": metadata},
        "response": {"claimsAndScopeOverrideDetails": None},
    }


def added_claims(event):
    details = event["response"]["claimsAndScopeOverrideDetails"] or {}
    return (details.get("accessTokenGeneration") or {}).get("claimsToAddOrOverride")


@pytest.mark.parametrize(
    "source",
    [
        "TokenGeneration_HostedAuth",
        "TokenGeneration_Authentication",
        "TokenGeneration_RefreshTokens",
        "TokenGeneration_NewPasswordChallenge",
    ],
)
def test_user_token_events_pass_through_untouched(source):
    event = {
        "triggerSource": source,
        "callerContext": {"clientId": "spa"},
        # garbage that would raise if the handler parsed it
        "request": {"clientMetadata": {"cei_user_sub": "not-a-uuid"}},
        "response": {"claimsAndScopeOverrideDetails": None},
    }
    before = copy.deepcopy(event)
    assert trigger.handler(event, None) == before


def test_router_token_gets_user_and_session_claims():
    event = trigger.handler(
        m2m_event({"cei_user_sub": SUB, "cei_session_id": "01a19b2c-abc"}), None
    )
    assert added_claims(event) == {"cei_user_sub": SUB, "cei_session_id": "01a19b2c-abc"}


def test_router_token_without_metadata_is_issued_without_claims():
    event = trigger.handler(m2m_event(None), None)
    assert added_claims(event) is None


def test_partial_metadata_adds_only_that_claim():
    event = trigger.handler(m2m_event({"cei_session_id": "s-1"}), None)
    assert added_claims(event) == {"cei_session_id": "s-1"}


@pytest.mark.parametrize(
    "metadata",
    [
        {"cei_user_sub": "not-a-uuid"},
        {"cei_user_sub": SUB, "cei_session_id": "../../etc"},
        {"cei_session_id": "x" * 65},
        {"cei_user_sub": 123},
    ],
)
def test_malformed_metadata_refuses_the_token(metadata):
    with pytest.raises(trigger.InvalidClientMetadata):
        trigger.handler(m2m_event(metadata), None)




def test_existing_overrides_are_kept():
    event = m2m_event({"cei_user_sub": SUB})
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {
            "claimsToAddOrOverride": {"other": "x"},
            "scopesToSuppress": ["a"],
        }
    }
    access = trigger.handler(event, None)["response"]["claimsAndScopeOverrideDetails"][
        "accessTokenGeneration"
    ]
    assert access == {
        "claimsToAddOrOverride": {"other": "x", "cei_user_sub": SUB},
        "scopesToSuppress": ["a"],
    }


def test_logs_never_contain_the_metadata_values(caplog):
    caplog.set_level("INFO")
    trigger.handler(m2m_event({"cei_user_sub": SUB, "cei_session_id": "secret-session"}), None)
    assert SUB not in caplog.text and "secret-session" not in caplog.text


# --- B112: user-token age-verification branch ----------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "TokenGeneration_HostedAuth",
        "TokenGeneration_Authentication",
        "TokenGeneration_RefreshTokens",
    ],
)
def test_adult_birthdate_adds_the_age_verified_claim(source):
    event = trigger.handler(user_event(source, {"birthdate": ADULT_BIRTHDATE}), None)
    assert added_claims(event) == {"cei_age_verified": "true"}


def test_exactly_18_today_is_adult():
    birthdate = TODAY.replace(year=TODAY.year - 18).isoformat()
    event = trigger.handler(user_event(user_attributes={"birthdate": birthdate}), None)
    assert added_claims(event) == {"cei_age_verified": "true"}


def test_one_day_short_of_18_is_not_adult():
    birthdate = (TODAY.replace(year=TODAY.year - 18) + timedelta(days=1)).isoformat()
    event = trigger.handler(user_event(user_attributes={"birthdate": birthdate}), None)
    assert added_claims(event) is None


def test_underage_birthdate_adds_no_claim():
    event = trigger.handler(user_event(user_attributes={"birthdate": MINOR_BIRTHDATE}), None)
    assert added_claims(event) is None


def test_missing_birthdate_adds_no_claim():
    event = trigger.handler(user_event(user_attributes={}), None)
    assert added_claims(event) is None


@pytest.mark.parametrize("birthdate", ["not-a-date", "2020/01/01", "", 20200101, None])
def test_malformed_birthdate_is_safe_and_adds_no_claim(birthdate):
    event = trigger.handler(user_event(user_attributes={"birthdate": birthdate}), None)
    assert added_claims(event) is None


def test_user_token_without_request_key_is_safe():
    event = {"triggerSource": "TokenGeneration_Authentication", "callerContext": {"clientId": "spa"},
             "response": {"claimsAndScopeOverrideDetails": None}}
    result = trigger.handler(event, None)
    assert added_claims(result) is None


def test_existing_overrides_are_kept_for_a_user_token():
    event = user_event(user_attributes={"birthdate": ADULT_BIRTHDATE})
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {"claimsToAddOrOverride": {"other": "x"}, "scopesToSuppress": ["a"]}
    }
    access = trigger.handler(event, None)["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]
    assert access == {
        "claimsToAddOrOverride": {"other": "x", "cei_age_verified": "true"},
        "scopesToSuppress": ["a"],
    }


def test_user_token_original_event_is_never_mutated():
    event = user_event(user_attributes={"birthdate": ADULT_BIRTHDATE})
    before = copy.deepcopy(event)
    result = trigger.handler(event, None)
    assert event == before  # the input event itself is untouched
    assert result is not event
    assert added_claims(result) == {"cei_age_verified": "true"}


def test_logs_never_contain_the_birthdate(caplog):
    caplog.set_level("INFO")
    trigger.handler(user_event(user_attributes={"birthdate": ADULT_BIRTHDATE}), None)
    assert ADULT_BIRTHDATE not in caplog.text
