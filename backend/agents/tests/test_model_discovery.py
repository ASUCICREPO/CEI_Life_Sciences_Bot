from unittest.mock import Mock

from shared.runtime import model_discovery
from shared.runtime.model_discovery import DiscoveredModel, discover_models, is_model_available, permitted_models


def _client(foundation=None, profiles=None, get_profile=None):
    client = Mock()
    client.list_foundation_models.return_value = {"modelSummaries": foundation or []}
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": profiles or []}
    if get_profile is not None:
        client.get_inference_profile.side_effect = get_profile
    return client


def setup_function(_fn):
    model_discovery._cache.clear()


def test_discovers_foundation_models_with_labels():
    client = _client(foundation=[{"modelId": "anthropic.claude-sonnet-v1", "modelName": "Claude Sonnet"}])
    assert discover_models(client) == [DiscoveredModel("anthropic.claude-sonnet-v1", "Claude Sonnet")]


def test_resolves_profile_already_naming_one_model():
    client = _client(
        foundation=[{"modelId": "anthropic.claude-sonnet-v1", "modelName": "Claude Sonnet"}],
        profiles=[{
            "inferenceProfileId": "us.anthropic.claude-sonnet-v1",
            "inferenceProfileName": "US Sonnet",
            "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-v1"}],
        }],
    )
    models = discover_models(client)
    assert DiscoveredModel("us.anthropic.claude-sonnet-v1", "US Sonnet") in models
    client.get_inference_profile.assert_not_called()


def test_falls_back_to_get_inference_profile():
    client = _client(
        foundation=[{"modelId": "anthropic.claude-sonnet-v1"}],
        profiles=[{"inferenceProfileId": "custom.sonnet", "inferenceProfileName": "Custom"}],
        get_profile=lambda **_: {
            "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-v1"}],
        },
    )
    models = discover_models(client)
    assert any(m.model_id == "custom.sonnet" for m in models)


def test_excludes_unresolvable_opaque_profile_fail_closed():
    client = _client(
        foundation=[{"modelId": "anthropic.claude-sonnet-v1"}],
        profiles=[{"inferenceProfileId": "opaque.profile"}],
        get_profile=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    models = discover_models(client)
    assert all(m.model_id != "opaque.profile" for m in models)


def test_excludes_multi_model_profile_fail_closed():
    client = _client(
        foundation=[{"modelId": "anthropic.claude-sonnet-v1"}, {"modelId": "anthropic.claude-haiku-v1"}],
        profiles=[{
            "inferenceProfileId": "multi.profile",
            "models": [
                {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-v1"},
                {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-v1"},
            ],
        }],
    )
    models = discover_models(client)
    assert all(m.model_id != "multi.profile" for m in models)


def test_excludes_profile_resolving_outside_the_foundation_set():
    client = _client(
        foundation=[{"modelId": "anthropic.claude-sonnet-v1"}],
        profiles=[{
            "inferenceProfileId": "stale.profile",
            "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/unknown.model-v1"}],
        }],
    )
    models = discover_models(client)
    assert all(m.model_id != "stale.profile" for m in models)


def test_permitted_models_filters_blocklist():
    client = _client(foundation=[
        {"modelId": "anthropic.claude-sonnet-v1"}, {"modelId": "anthropic.claude-fable-5-v1"},
    ])
    result = permitted_models(client, ["anthropic.claude-fable-5*"])
    assert [m.model_id for m in result] == ["anthropic.claude-sonnet-v1"]


def test_is_model_available_checks_blocklist_and_discovery():
    client = _client(foundation=[{"modelId": "anthropic.claude-sonnet-v1"}])
    assert is_model_available("anthropic.claude-sonnet-v1", client, [])
    assert not is_model_available("anthropic.claude-sonnet-v1", client, ["anthropic.claude-sonnet*"])
    assert not is_model_available("unknown.model", client, [])


def test_discovery_result_is_cached_per_key():
    client = _client(foundation=[{"modelId": "anthropic.claude-sonnet-v1"}])
    first = permitted_models(client, [], cache_key="k1")
    client.list_foundation_models.return_value = {"modelSummaries": []}
    second = permitted_models(client, [], cache_key="k1")
    assert first == second
    third = permitted_models(client, [], cache_key="k2")
    assert third == []
