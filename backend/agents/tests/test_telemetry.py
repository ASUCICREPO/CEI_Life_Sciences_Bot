"""Exercise runtime instrumentation in a clean interpreter before SDK imports."""
import os
import subprocess
import sys

from shared.telemetry import configure_telemetry


def test_adot_bootstrap_instruments_sdk_without_http_clients():
    env = os.environ.copy()
    env.update({"CEI_TELEMETRY_ENABLED": "true", "OTEL_PYTHON_DISTRO": "aws_distro",
                "OTEL_PYTHON_CONFIGURATOR": "aws_configurator", "OTEL_TRACES_EXPORTER": "none",
                "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
                "AGENT_OBSERVABILITY_ENABLED": "true", "AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT": "false",
                "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": "requests,urllib,urllib3,httpx,httpx2,aiohttp-client"})
    code = (
        "from shared.telemetry import configure_telemetry; configure_telemetry(); "
        "from opentelemetry.instrumentation.botocore import BotocoreInstrumentor; "
        "from opentelemetry.instrumentation.requests import RequestsInstrumentor; "
        "from opentelemetry.propagate import get_global_textmap; "
        "from opentelemetry._logs import get_logger_provider; "
        "import logging; logging.getLogger('telemetry_test').error('LOCAL_LOG_RETAINED'); "
        "assert BotocoreInstrumentor().is_instrumented_by_opentelemetry; "
        "assert not RequestsInstrumentor().is_instrumented_by_opentelemetry; "
        "assert 'X-Amzn-Trace-Id' in get_global_textmap().fields; "
        "assert not get_logger_provider()._multi_log_record_processor._log_record_processors"
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "telemetry_initialization_failed" not in result.stderr
    assert "LOCAL_LOG_RETAINED" in result.stderr


def test_initialization_failure_does_not_stop_application(monkeypatch, caplog):
    from opentelemetry.instrumentation import auto_instrumentation

    def fail(**kwargs):
        raise RuntimeError("private connection details")

    monkeypatch.setenv("CEI_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(auto_instrumentation, "initialize", fail)
    configure_telemetry()
    assert "telemetry_initialization_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private connection details" not in caplog.text


def test_aws_span_exporter_removes_genai_content(monkeypatch):
    from amazon.opentelemetry.distro.exporter.otlp.aws.traces.otlp_aws_span_exporter import OTLPAwsSpanExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("AGENT_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT", "false")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    seen = []
    monkeypatch.setattr(OTLPSpanExporter, "export", lambda _self, spans: seen.extend(spans) or SpanExportResult.SUCCESS)
    provider = LoggerProvider()  # no processors: extracted content is discarded
    exporter = OTLPAwsSpanExporter(aws_region="us-east-1", endpoint="http://localhost:4318/v1/traces", logger_provider=provider)
    memory = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(memory))
    with tracer_provider.get_tracer("test").start_as_current_span("test") as active:
        active.set_attribute("gen_ai.input.messages", "private prompt")
        active.set_attribute("operation", "research")
    span = memory.get_finished_spans()[0]
    assert exporter.export([span]) is SpanExportResult.SUCCESS
    assert seen[0].attributes.get("operation") == "research"
    assert "private prompt" not in str(seen[0].attributes)
