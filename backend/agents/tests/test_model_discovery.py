from unittest.mock import Mock

from shared.runtime import model_discovery
from shared.runtime.model_discovery import DiscoveredModel, discover_models, is_model_available, permitted_models


def _qualifying(model_id, **overrides):
    """A foundation-model summary that passes every tool-capability check (overridable)."""
    summary = {
        "modelId": model_id,
        "outputModalities": ["TEXT"],
        "inputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "inferenceTypesSupported": ["ON_DEMAND"],
    }
    summary.update(overrides)
    return summary


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
    client = _client(foundation=[_qualifying("anthropic.claude-sonnet-v1", modelName="Claude Sonnet")])
    assert discover_models(client) == [DiscoveredModel("anthropic.claude-sonnet-v1", "Claude Sonnet")]


def test_resolves_profile_already_naming_one_model():
    client = _client(
        foundation=[_qualifying("anthropic.claude-sonnet-v1", modelName="Claude Sonnet")],
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
        foundation=[_qualifying("anthropic.claude-sonnet-v1")],
        profiles=[{"inferenceProfileId": "custom.sonnet", "inferenceProfileName": "Custom"}],
        get_profile=lambda **_: {
            "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-v1"}],
        },
    )
    models = discover_models(client)
    assert any(m.model_id == "custom.sonnet" for m in models)


def test_excludes_unresolvable_opaque_profile_fail_closed():
    client = _client(
        foundation=[_qualifying("anthropic.claude-sonnet-v1")],
        profiles=[{"inferenceProfileId": "opaque.profile"}],
        get_profile=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    models = discover_models(client)
    assert all(m.model_id != "opaque.profile" for m in models)


def test_excludes_multi_model_profile_fail_closed():
    client = _client(
        foundation=[_qualifying("anthropic.claude-sonnet-v1"), _qualifying("anthropic.claude-haiku-v1")],
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
        foundation=[_qualifying("anthropic.claude-sonnet-v1")],
        profiles=[{
            "inferenceProfileId": "stale.profile",
            "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/unknown.model-v1"}],
        }],
    )
    models = discover_models(client)
    assert all(m.model_id != "stale.profile" for m in models)


def test_permitted_models_filters_blocklist():
    client = _client(foundation=[
        _qualifying("anthropic.claude-sonnet-v1"), _qualifying("anthropic.claude-fable-5-v1"),
    ])
    result = permitted_models(client, ["anthropic.claude-fable-5*"])
    assert [m.model_id for m in result] == ["anthropic.claude-sonnet-v1"]


def test_is_model_available_checks_blocklist_and_discovery():
    client = _client(foundation=[_qualifying("anthropic.claude-sonnet-v1")])
    assert is_model_available("anthropic.claude-sonnet-v1", client, [])
    assert not is_model_available("anthropic.claude-sonnet-v1", client, ["anthropic.claude-sonnet*"])
    assert not is_model_available("unknown.model", client, [])


def test_discovery_result_is_cached_per_key():
    client = _client(foundation=[_qualifying("anthropic.claude-sonnet-v1")])
    first = permitted_models(client, [], cache_key="k1")
    client.list_foundation_models.return_value = {"modelSummaries": []}
    second = permitted_models(client, [], cache_key="k1")
    assert first == second
    third = permitted_models(client, [], cache_key="k2")
    assert third == []


def test_excludes_embedding_model_missing_text_output():
    client = _client(foundation=[_qualifying(
        "amazon.titan-embed-text-v2", outputModalities=["EMBEDDING"], responseStreamingSupported=False,
    )])
    assert discover_models(client) == []


def test_excludes_image_model_missing_text_input():
    client = _client(foundation=[_qualifying(
        "amazon.nova-canvas-v1:0", inputModalities=["TEXT", "IMAGE"], outputModalities=["IMAGE"],
        responseStreamingSupported=False,
    )])
    assert discover_models(client) == []


def test_excludes_non_streaming_model():
    client = _client(foundation=[_qualifying("anthropic.claude-sonnet-v1", responseStreamingSupported=False)])
    assert discover_models(client) == []


def test_excludes_provisioned_only_model():
    client = _client(foundation=[_qualifying(
        "anthropic.claude-sonnet-v1", inferenceTypesSupported=["PROVISIONED"],
    )])
    assert discover_models(client) == []


def test_excludes_non_allowlisted_provider():
    client = _client(foundation=[_qualifying("stability.stable-diffusion-xl-v1")])
    assert discover_models(client) == []


def test_excludes_legacy_model():
    client = _client(foundation=[_qualifying(
        "anthropic.claude-sonnet-v1", modelLifecycle={"status": "LEGACY"},
    )])
    assert discover_models(client) == []


def test_includes_allowlisted_llama_and_nova_and_claude():
    client = _client(foundation=[
        _qualifying("anthropic.claude-sonnet-v1"),
        _qualifying("amazon.nova-pro-v1:0"),
        _qualifying("meta.llama3-1-70b-instruct-v1:0"),
    ])
    model_ids = {m.model_id for m in discover_models(client)}
    assert model_ids == {"anthropic.claude-sonnet-v1", "amazon.nova-pro-v1:0", "meta.llama3-1-70b-instruct-v1:0"}


def test_includes_google_moonshot_zai_and_xai_tool_capable_models():
    client = _client(foundation=[
        _qualifying("google.gemma-3-12b-it"),
        _qualifying("moonshotai.kimi-k3"),
        _qualifying("moonshot.kimi-k2-thinking"),
        _qualifying("zai.glm-5"),
        _qualifying("xai.grok-4-7"),
    ])
    model_ids = {m.model_id for m in discover_models(client)}
    assert model_ids == {
        "google.gemma-3-12b-it", "moonshotai.kimi-k3", "moonshot.kimi-k2-thinking",
        "zai.glm-5", "xai.grok-4-7",
    }


def test_excludes_google_gemma_pretrained_variant():
    client = _client(foundation=[_qualifying("google.gemma-3-27b-pt")])
    assert discover_models(client) == []


def test_excludes_profile_resolving_to_non_qualifying_model():
    client = _client(
        foundation=[_qualifying("stability.stable-diffusion-xl-v1")],
        profiles=[{
            "inferenceProfileId": "us.stability.stable-diffusion-xl-v1",
            "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/stability.stable-diffusion-xl-v1"}],
        }],
    )
    models = discover_models(client)
    assert models == []


def test_resolves_cross_region_profile_listing_one_arn_per_region():
    # Real us./global. profiles list one ARN per Region, all for the same model (checked live 2026-11-18).
    model_id = "anthropic.claude-sonnet-v1"
    client = _client(
        foundation=[_qualifying(model_id)],
        profiles=[{
            "inferenceProfileId": "us." + model_id,
            "inferenceProfileName": "US Sonnet",
            "models": [
                {"modelArn": "arn:aws:bedrock:%s::foundation-model/%s" % (region, model_id)}
                for region in ("us-east-1", "us-east-2", "us-west-2")
            ],
        }],
    )
    assert DiscoveredModel("us." + model_id, "US Sonnet") in discover_models(client)
    client.get_inference_profile.assert_not_called()
