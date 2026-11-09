import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
import requests
from moto import mock_aws
from pydantic import BaseModel, Field

LAMBDAS = Path(__file__).resolve().parents[2]
# ADR: same path setup as the session handler tests | Rationale: `shared.tools` comes from backend/agents
# (the data-source layer ships a subset of it) and `data_source_runtime` from lambdas/shared
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS.parent / "agents"))

import data_source_runtime as runtime  # noqa: E402
from shared.schemas.citation import SourceType  # noqa: E402
from shared.tools import retrying_http  # noqa: E402
from shared.tools.base import DataSourceResult, DataSourceTool  # noqa: E402
from shared.tools.data_sources.pubmed import PubMedTool  # noqa: E402

USER_ID = "3b1f6c1e-1d2a-4c5b-9e8f-0123456789ab"

# (target dir id, tool class name, user-key secret source id)
SOURCES = [
    ("pubmed", "PubMedTool", "ncbi"),
    ("clinicaltrials", "ClinicalTrialsTool", None),
    ("biorxiv", "BioRxivTool", None),
    ("medrxiv", "MedRxivTool", None),
    ("chembl", "ChEMBLTool", None),
    ("opentargets", "OpenTargetsTool", None),
    ("cmscoverage", "CMSCoverageTool", None),
    ("icd10", "ICD10Tool", None),
    ("npi", "NPIRegistryTool", None),
    ("pubchem", "PubChemTool", None),
    ("openfda", "OpenFDATool", None),
    ("patentsview", "PatentsViewTool", None),
]


def gateway_context(tool_name: str | None = "src-test___search") -> Any:
    custom = {"bedrockAgentCoreToolName": tool_name} if tool_name else {}
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom), aws_request_id="req-1")


def load_handler_module(source_id: str):
    path = LAMBDAS / f"data-source-{source_id}" / "handler.py"
    spec = importlib.util.spec_from_file_location(f"data_source_handler_{source_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTool(DataSourceTool):
    """Records fetch() kwargs; returns one canned result."""

    function_name = "fake"
    source_type = SourceType.PUBMED
    description = "fake"
    supports_api_key = True
    received: list[dict[str, Any]] = []

    class InputSchema(BaseModel):
        query: str
        max_results: int = Field(default=10, ge=1, le=100)

    def _build_payload(self, **params: Any) -> dict[str, Any]:
        return params

    def fetch(self, **params: Any) -> DataSourceResult:
        FakeTool.received.append(params)
        return DataSourceResult(
            source_type=self.source_type,
            results=[{"id": 1}],
            citations=[],
            query_metadata={"query": params["query"]},
        )


class FakeKeylessTool(FakeTool):
    supports_api_key = False


@pytest.fixture(autouse=True)
def reset_fake():
    FakeTool.received = []


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-east-1")


# ---------------------------------------------------------------- handler wiring


def handler_closure(source_id: str) -> dict[str, Any]:
    handler = load_handler_module(source_id).handler
    return {
        name: cell.cell_contents
        for name, cell in zip(handler.__code__.co_freevars, handler.__closure__)
    }


@pytest.mark.parametrize("source_id,class_name,key_source", SOURCES)
def test_every_handler_wraps_its_tool(source_id, class_name, key_source):
    closure = handler_closure(source_id)
    assert type(closure["tool"]).__name__ == class_name
    assert closure["source_id"] == source_id
    assert closure["key_source_id"] == (key_source or source_id)


def test_handlers_match_the_source_registry():
    from shared.tools.data_sources import TOOLS_BY_SOURCE_ID

    assert {sid for sid, _, _ in SOURCES} == set(TOOLS_BY_SOURCE_ID)
    for sid, _, _ in SOURCES:
        assert type(handler_closure(sid)["tool"]) is TOOLS_BY_SOURCE_ID[sid]


def test_only_pubmed_takes_a_user_key():
    keyed = [sid for sid, _, _ in SOURCES if handler_closure(sid)["tool"].supports_api_key]
    assert keyed == ["pubmed"]


@pytest.mark.parametrize("source_id,class_name,_key", SOURCES)
def test_every_source_goes_through_the_retry_helper(monkeypatch, source_id, class_name, _key):
    """An upstream outage becomes a DataSourceResult error, never an exception, for all ten sources."""
    calls: list[str] = []

    def down(method, url, **kwargs):
        calls.append(url)
        raise requests.ConnectionError("upstream down")

    monkeypatch.setattr(retrying_http, "request", down)
    handler = load_handler_module(source_id).handler
    args = {"npi": {"name": "smith"}, "opentargets": {"target": "EGFR"}}.get(
        source_id, {"query": "aspirin"}
    )
    payload = handler(args, gateway_context(f"{source_id}___search"))
    assert calls, f"{class_name} did not call retrying_http"
    parsed = DataSourceResult.model_validate(payload)
    assert parsed.results == [] and parsed.citations == []
    assert "upstream down" in parsed.query_metadata["error"]


# ---------------------------------------------------------------- request handling


def test_valid_call_returns_data_source_result_shape():
    handler = runtime.make_handler(FakeTool, source_id="test")
    payload = handler({"query": "aspirin", "max_results": 5}, gateway_context())
    parsed = DataSourceResult.model_validate(payload)
    assert parsed.results == [{"id": 1}]
    assert payload["query_metadata"] == {"query": "aspirin", "user_api_key": False}
    assert FakeTool.received == [{"query": "aspirin", "max_results": 5}]
    json.dumps(payload)  # the Gateway needs plain JSON


def test_invalid_input_is_reported_not_raised():
    handler = runtime.make_handler(FakeTool, source_id="test")
    payload = handler({"max_results": 500}, gateway_context())
    meta = DataSourceResult.model_validate(payload).query_metadata
    assert meta["error"] == "invalid_input"
    assert meta["fields"] == ["max_results", "query"]
    assert FakeTool.received == []


def test_non_object_arguments_are_rejected():
    handler = runtime.make_handler(FakeTool, source_id="test")
    assert handler(["aspirin"], gateway_context())["query_metadata"]["error"] == "invalid_input"


def test_unknown_tool_name_is_rejected():
    handler = runtime.make_handler(FakeTool, source_id="test")
    payload = handler({"query": "x"}, gateway_context("src-test___delete"))
    assert payload["query_metadata"]["error"] == "unknown_tool"
    assert FakeTool.received == []


def test_missing_client_context_still_serves_the_call():
    handler = runtime.make_handler(FakeTool, source_id="test")
    assert handler({"query": "x"}, SimpleNamespace())["results"] == [{"id": 1}]


def test_agent_supplied_api_key_and_user_id_are_dropped():
    handler = runtime.make_handler(FakeTool, source_id="test")
    handler({"query": "x", "api_key": "stolen", "user_id": USER_ID}, gateway_context())
    assert FakeTool.received == [{"query": "x", "max_results": 10}]


def test_invalid_source_id_is_refused_at_build_time():
    with pytest.raises(ValueError):
        runtime.make_handler(FakeTool, source_id="../etc")


# ---------------------------------------------------------------- user API keys (B20/B78)


def test_default_resolver_never_uses_a_user_key(aws_env):
    aws_env.create_secret(Name=f"cei/user-keys/{USER_ID}/test", SecretString="k-123")
    handler = runtime.make_handler(
        FakeTool, source_id="test", secrets_client_factory=lambda: aws_env
    )
    payload = handler({"query": "x"}, gateway_context())
    assert "api_key" not in FakeTool.received[0]
    assert payload["query_metadata"]["user_api_key"] is False


@pytest.mark.parametrize("secret_string", ["k-123", '{"api_key": "k-123"}', "  k-123\n"])
def test_configured_user_key_is_used(aws_env, secret_string):
    aws_env.create_secret(Name=f"cei/user-keys/{USER_ID}/ncbi", SecretString=secret_string)
    handler = runtime.make_handler(
        FakeTool,
        source_id="pubmed",
        api_key_source_id="ncbi",
        user_id_resolver=lambda e, c: USER_ID,
        secrets_client_factory=lambda: aws_env,
    )
    payload = handler({"query": "x"}, gateway_context())
    assert FakeTool.received[0]["api_key"] == "k-123"
    assert payload["query_metadata"]["user_api_key"] is True
    assert "k-123" not in json.dumps(payload)


def test_missing_user_key_falls_back_to_free_path(aws_env):
    handler = runtime.make_handler(
        FakeTool,
        source_id="test",
        user_id_resolver=lambda e, c: USER_ID,
        secrets_client_factory=lambda: aws_env,
    )
    payload = handler({"query": "x"}, gateway_context())
    assert "api_key" not in FakeTool.received[0]
    assert payload["results"] == [{"id": 1}]
    assert payload["query_metadata"]["user_api_key"] is False


def test_secrets_error_falls_back_to_free_path():
    class Broken:
        def get_secret_value(self, **_):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetSecretValue"
            )

    handler = runtime.make_handler(
        FakeTool,
        source_id="test",
        user_id_resolver=lambda e, c: USER_ID,
        secrets_client_factory=Broken,
    )
    assert handler({"query": "x"}, gateway_context())["query_metadata"]["user_api_key"] is False
    assert "api_key" not in FakeTool.received[0]


class _NoCallSecrets:
    def get_secret_value(self, **_):
        raise AssertionError("Secrets Manager must not be called")


@pytest.mark.parametrize("bad_user", ["../other-user", "a/b", "x" * 200, ""])
def test_malformed_user_id_never_reaches_secrets_manager(bad_user):
    handler = runtime.make_handler(
        FakeTool,
        source_id="test",
        user_id_resolver=lambda e, c: bad_user,
        secrets_client_factory=_NoCallSecrets,
    )
    assert handler({"query": "x"}, gateway_context())["query_metadata"]["user_api_key"] is False
    assert "api_key" not in FakeTool.received[0]


def test_keyless_tool_never_looks_up_a_key():
    handler = runtime.make_handler(
        FakeKeylessTool,
        source_id="test",
        user_id_resolver=lambda e, c: USER_ID,
        secrets_client_factory=_NoCallSecrets,
    )
    assert handler({"query": "x"}, gateway_context())["results"] == [{"id": 1}]


# ---------------------------------------------------------------- PubMed key plumbing


class _Resp:
    def __init__(self, status: int, body: Any = None, text: str = "", url: str = "") -> None:
        self.status_code, self._body, self.text, self.url, self.headers = (
            status,
            body,
            text,
            url,
            {},
        )

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: Bad Request for url: {self.url}"
            )


def test_pubmed_sends_key_on_both_calls(monkeypatch):
    seen: list[dict] = []
    xml = "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><ArticleTitle>T</ArticleTitle></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"

    def fake(method, url, **kwargs):
        seen.append(kwargs["params"])
        if "esearch" in url:
            return _Resp(200, {"esearchresult": {"idlist": ["1"]}})
        return _Resp(200, text=xml)

    monkeypatch.setattr(retrying_http, "request", fake)
    result = PubMedTool().fetch(query="aspirin", max_results=1, api_key="k-123")
    assert [p.get("api_key") for p in seen] == ["k-123", "k-123"]
    assert result.citations[0].source_id == "PMID:1"


def test_pubmed_without_key_sends_no_key_param(monkeypatch):
    seen: list[dict] = []

    def fake(method, url, **kwargs):
        seen.append(kwargs["params"])
        return _Resp(200, {"esearchresult": {"idlist": []}})

    monkeypatch.setattr(retrying_http, "request", fake)
    PubMedTool().fetch(query="aspirin")
    assert "api_key" not in seen[0]


def test_pubmed_redacts_key_from_errors(monkeypatch, caplog):
    def fake(method, url, **kwargs):
        return _Resp(400, url=f"{url}?term=aspirin&api_key=k-123")

    monkeypatch.setattr(retrying_http, "request", fake)
    caplog.set_level("ERROR")
    result = PubMedTool().fetch(query="aspirin", api_key="k-123")
    assert "k-123" not in result.query_metadata["error"]
    assert "[REDACTED]" in result.query_metadata["error"]
    assert "k-123" not in caplog.text
