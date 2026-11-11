import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("router_classifier", Path(__file__).parents[1] / "classifier.py")
classifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = classifier
spec.loader.exec_module(classifier)


def output(agent="research", confidence=0.9):
    return {"output": {"message": {"content": [{"toolUse": {"name": "select_specialist",
            "input": {"agent": agent, "confidence": confidence}}}]}}}


def run(client, **kwargs):
    return classifier.classify("user prompt", client=client, model_id="fixed-model", guardrail_id="guardrail",
                               guardrail_version="1", **kwargs)


@pytest.mark.parametrize("agent", classifier.AGENTS)
def test_classifies_using_fixed_model_and_input_guardrail(agent):
    client = Mock(converse=Mock(return_value=output(agent)))
    result = run(client)
    assert result.agent == agent and not result.needs_choice()
    args = client.converse.call_args.kwargs
    assert args["modelId"] == "fixed-model"
    assert args["guardrailConfig"] == {"guardrailIdentifier": "guardrail", "guardrailVersion": "1"}
    assert args["messages"][0]["content"][0]["guardContent"]["text"]["text"] == "user prompt"


def test_ambiguous_response_requires_choice():
    result = run(Mock(converse=Mock(return_value=output(confidence=0.69))))
    assert result.needs_choice()


@pytest.mark.parametrize("confidence", [True, -0.1, 1.1, float("nan"), "0.7"])
def test_rejects_bad_confidence(confidence):
    with pytest.raises(classifier.ClassificationError):
        run(Mock(converse=Mock(return_value=output(confidence=confidence))))


def test_missing_guardrail_fails_before_model_call():
    client = Mock()
    with pytest.raises(classifier.ClassificationError):
        classifier.classify("prompt", client=client, model_id="fixed", guardrail_id="", guardrail_version="1")
    client.converse.assert_not_called()


def test_guardrail_intervention_does_not_route():
    client = Mock(converse=Mock(return_value={"stopReason": "guardrail_intervened"}))
    with pytest.raises(classifier.GuardrailIntervened):
        run(client)


@pytest.mark.parametrize("response", [{}, output("unexpected"), {"output": {"message": {"content": []}}}])
def test_invalid_or_unknown_result_fails_closed(response):
    with pytest.raises(classifier.ClassificationError):
        run(Mock(converse=Mock(return_value=response)))
