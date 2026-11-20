from __future__ import annotations

from unittest.mock import Mock

from cei_deploy import models


def _fm(model_id, provider, **overrides):
    summary = {
        "modelId": model_id,
        "providerName": provider,
        "outputModalities": ["TEXT"],
        "inputModalities": ["TEXT", "IMAGE"],
        "responseStreamingSupported": True,
        "inferenceTypesSupported": ["ON_DEMAND", "INFERENCE_PROFILE"],
    }
    summary.update(overrides)
    return summary


def _profile(profile_id, name, underlying_arn):
    return {
        "inferenceProfileId": profile_id,
        "inferenceProfileName": name,
        "models": [{"modelArn": underlying_arn}],
    }


def _arn(model_id):
    return "arn:aws:bedrock:us-east-1::foundation-model/%s" % model_id


def _client(foundation=None, profiles=None, get_profile=None):
    client = Mock()
    client.list_foundation_models.return_value = {"modelSummaries": foundation or []}
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": profiles or []}
    if get_profile is not None:
        client.get_inference_profile.side_effect = get_profile
    return client


def test_provider_allowlist_filters_non_allowlisted_providers():
    client = _client(
        foundation=[
            _fm("anthropic.claude-sonnet-x", "Anthropic"),
            _fm("amazon.nova-pro-v1:0", "Amazon"),
        ],
        profiles=[
            _profile("global.anthropic.claude-sonnet-x", "Sonnet", _arn("anthropic.claude-sonnet-x")),
            _profile("global.amazon.nova-pro-v1:0", "Nova", _arn("amazon.nova-pro-v1:0")),
        ],
    )
    result = models.discover(client)
    ids = {item.base_model_id for item in result.all_items()}
    assert ids == {"anthropic.claude-sonnet-x"}
    # nova is still backend-discoverable (used for blockedModelIds), just not shown in the picker
    assert "amazon.nova-pro-v1:0" in result.all_backend_ids


def test_providers_matched_by_normalized_name_or_id_prefix():
    client = _client(
        foundation=[
            _fm("zai.glm-5", "Z.AI"),
            _fm("xai.grok-4-7", "xAI"),
            _fm("moonshotai.kimi-k3", "Moonshot AI"),
            _fm("google.gemma-3-12b-it", "Google"),
        ],
        profiles=[
            _profile("global.zai.glm-5", "GLM", _arn("zai.glm-5")),
            _profile("us.xai.grok-4-7", "Grok", _arn("xai.grok-4-7")),
            _profile("global.moonshotai.kimi-k3", "Kimi", _arn("moonshotai.kimi-k3")),
            _profile("us.google.gemma-3-12b-it", "Gemma", _arn("google.gemma-3-12b-it")),
        ],
    )
    result = models.discover(client)
    ids = {item.base_model_id for item in result.all_items()}
    assert ids == {"zai.glm-5", "xai.grok-4-7", "moonshotai.kimi-k3", "google.gemma-3-12b-it"}


def test_only_global_and_us_profile_prefixes_are_kept():
    client = _client(
        foundation=[_fm("anthropic.claude-sonnet-x", "Anthropic")],
        profiles=[
            _profile("global.anthropic.claude-sonnet-x", "Sonnet Global", _arn("anthropic.claude-sonnet-x")),
            _profile("us.anthropic.claude-sonnet-x", "Sonnet US", _arn("anthropic.claude-sonnet-x")),
            _profile("eu.anthropic.claude-sonnet-x", "Sonnet EU", _arn("anthropic.claude-sonnet-x")),
        ],
    )
    result = models.discover(client)
    assert len(result.global_items) == 1
    assert len(result.us_items) == 1


def test_get_inference_profile_fallback_runs_for_unresolved_profiles():
    client = _client(
        foundation=[_fm("anthropic.claude-sonnet-x", "Anthropic")],
        profiles=[{"inferenceProfileId": "global.custom", "inferenceProfileName": "Custom"}],
        get_profile=lambda **_: {"models": [{"modelArn": _arn("anthropic.claude-sonnet-x")}]},
    )
    result = models.discover(client)
    assert any(item.profile_id == "global.custom" for item in result.all_items())


def test_sorts_newest_first_by_numeric_parts():
    client = _client(
        foundation=[
            _fm("anthropic.claude-sonnet-4-5-20250929-v1:0", "Anthropic"),
            _fm("anthropic.claude-sonnet-5", "Anthropic"),
        ],
        profiles=[
            _profile("global.old", "Old Sonnet", _arn("anthropic.claude-sonnet-4-5-20250929-v1:0")),
            _profile("global.new", "New Sonnet", _arn("anthropic.claude-sonnet-5")),
        ],
    )
    result = models.discover(client)
    assert [item.profile_id for item in result.global_items] == ["global.old", "global.new"][::-1]


def test_smart_defaults_prefer_global_sonnet_and_haiku():
    client = _client(
        foundation=[
            _fm("anthropic.claude-sonnet-x", "Anthropic"),
            _fm("anthropic.claude-haiku-x", "Anthropic"),
        ],
        profiles=[
            _profile("us.anthropic.claude-sonnet-x", "Sonnet US", _arn("anthropic.claude-sonnet-x")),
            _profile("global.anthropic.claude-sonnet-x", "Sonnet Global", _arn("anthropic.claude-sonnet-x")),
            _profile("global.anthropic.claude-haiku-x", "Haiku Global", _arn("anthropic.claude-haiku-x")),
        ],
    )
    result = models.discover(client)
    assert result.default_model_id == "global.anthropic.claude-sonnet-x"
    assert result.router_model_id == "global.anthropic.claude-haiku-x"


def test_fallback_default_when_no_sonnet_or_haiku():
    client = _client(
        foundation=[_fm("qwen.qwen3-32b-v1:0", "Qwen")],
        profiles=[_profile("global.qwen", "Qwen", _arn("qwen.qwen3-32b-v1:0"))],
    )
    result = models.discover(client)
    assert result.default_model_id == "global.qwen"
    assert result.router_model_id == "global.qwen"


def test_compute_blocked_model_ids_blocks_bare_id_when_nothing_checked():
    all_ids = ["amazon.nova-pro-v1:0", "anthropic.claude-sonnet-x", "qwen.qwen3-32b-v1:0"]
    items = [
        models.ModelItem("global.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
        models.ModelItem("us.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
    ]
    assert models.compute_blocked_model_ids(all_ids, items, []) == [
        "amazon.nova-pro-v1:0", "anthropic.claude-sonnet-x", "qwen.qwen3-32b-v1:0",
    ]


def test_compute_blocked_model_ids_only_us_checked_blocks_global_profile_only():
    all_ids = ["anthropic.claude-sonnet-x"]
    items = [
        models.ModelItem("global.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
        models.ModelItem("us.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
    ]
    checked = ["us.anthropic.claude-sonnet-x"]
    blocked = models.compute_blocked_model_ids(all_ids, items, checked)
    assert blocked == ["global.anthropic.claude-sonnet-x"]
    assert "anthropic.claude-sonnet-x" not in blocked  # bare id stays allowed


def test_compute_blocked_model_ids_both_checked_blocks_nothing():
    all_ids = ["anthropic.claude-sonnet-x"]
    items = [
        models.ModelItem("global.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
        models.ModelItem("us.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
    ]
    checked = ["global.anthropic.claude-sonnet-x", "us.anthropic.claude-sonnet-x"]
    assert models.compute_blocked_model_ids(all_ids, items, checked) == []


def test_checked_profile_ids_from_blocklist_round_trips_compute():
    all_ids = ["anthropic.claude-sonnet-x", "qwen.qwen3-32b-v1:0"]
    items = [
        models.ModelItem("global.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
        models.ModelItem("us.anthropic.claude-sonnet-x", "A", "anthropic.claude-sonnet-x", "Anthropic"),
        models.ModelItem("global.qwen.qwen3-32b-v1:0", "B", "qwen.qwen3-32b-v1:0", "Qwen"),
    ]
    checked = ["global.anthropic.claude-sonnet-x"]
    blocked = models.compute_blocked_model_ids(all_ids, items, checked)
    round_tripped = models.checked_profile_ids_from_blocklist(items, blocked)
    assert round_tripped == checked


def test_is_blocked_unprefixed_pattern_blocks_every_prefix_and_bare_id():
    assert models.is_blocked("us.anthropic.claude-sonnet-x", ["anthropic.claude-sonnet-x"])
    assert models.is_blocked("global.anthropic.claude-sonnet-x", ["anthropic.claude-sonnet-x"])
    assert models.is_blocked("anthropic.claude-sonnet-x", ["anthropic.claude-sonnet-x"])
    assert not models.is_blocked("anthropic.claude-haiku-x", ["anthropic.claude-sonnet-x"])


def test_is_blocked_prefixed_pattern_blocks_only_that_prefix():
    assert models.is_blocked("us.anthropic.claude-sonnet-x", ["us.anthropic.claude-sonnet-x"])
    assert not models.is_blocked("global.anthropic.claude-sonnet-x", ["us.anthropic.claude-sonnet-x"])
    assert not models.is_blocked("anthropic.claude-sonnet-x", ["us.anthropic.claude-sonnet-x"])


def test_is_blocked_handles_arn_and_empty_id():
    arn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-x"
    assert models.is_blocked(arn, ["anthropic.claude-sonnet-x"])
    assert models.is_blocked("", ["anthropic.claude-sonnet-x"])
    assert models.is_blocked("   ", ["anthropic.claude-sonnet-x"])
    assert models.is_blocked(None, ["anthropic.claude-sonnet-x"])


def test_multi_model_profile_is_excluded():
    client = _client(
        foundation=[_fm("anthropic.claude-sonnet-x", "Anthropic"), _fm("anthropic.claude-haiku-x", "Anthropic")],
        profiles=[{
            "inferenceProfileId": "global.multi",
            "models": [{"modelArn": _arn("anthropic.claude-sonnet-x")}, {"modelArn": _arn("anthropic.claude-haiku-x")}],
        }],
    )
    result = models.discover(client)
    assert all(item.profile_id != "global.multi" for item in result.all_items())


def test_discovery_error_on_client_failure():
    client = Mock()
    client.list_foundation_models.side_effect = RuntimeError("AccessDenied")
    try:
        models.discover(client)
        assert False, "expected ModelDiscoveryError"
    except models.ModelDiscoveryError as exc:
        assert "ListFoundationModels" in models.ModelDiscoveryError.REQUIRED_PERMISSIONS[0]
        assert exc.cause is not None


def test_text_only_models_are_hidden_but_blocked():
    client = _client(
        foundation=[
            _fm("anthropic.claude-sonnet-x", "Anthropic"),
            _fm("qwen.text-only", "Qwen", inputModalities=["TEXT"]),
        ],
        profiles=[
            _profile("global.anthropic.claude-sonnet-x", "Sonnet", _arn("anthropic.claude-sonnet-x")),
            _profile("global.qwen.text-only", "Qwen", _arn("qwen.text-only")),
        ],
    )
    result = models.discover(client)
    assert [i.profile_id for i in result.global_items] == ["global.anthropic.claude-sonnet-x"]
    blocked = models.compute_blocked_model_ids(
        result.all_backend_ids, result.all_items(), ["global.anthropic.claude-sonnet-x"],
    )
    assert blocked == ["qwen.text-only"]


def test_d3_models_are_never_listed():
    client = _client(
        foundation=[_fm("anthropic.claude-fable-5-2", "Anthropic")],
        profiles=[_profile("global.anthropic.claude-fable-5-2", "Fable", _arn("anthropic.claude-fable-5-2"))],
    )
    assert models.discover(client).global_items == []


def test_cross_region_profile_with_one_arn_per_region_resolves():
    model_id = "anthropic.claude-sonnet-x"
    profile = {
        "inferenceProfileId": "us." + model_id,
        "inferenceProfileName": "US Sonnet",
        "models": [
            {"modelArn": "arn:aws:bedrock:%s::foundation-model/%s" % (region, model_id)}
            for region in ("us-east-1", "us-east-2", "us-west-2")
        ],
    }
    result = models.discover(_client(foundation=[_fm(model_id, "Anthropic")], profiles=[profile]))
    assert [i.profile_id for i in result.us_items] == ["us." + model_id]


def test_profile_with_different_models_stays_unresolved():
    profile = {"models": [{"modelArn": _arn("a.one")}, {"modelArn": _arn("a.two")}]}
    assert models._profile_underlying_model(profile) is None
