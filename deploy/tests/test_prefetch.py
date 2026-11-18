from __future__ import annotations

from unittest.mock import Mock

from cei_deploy import prefetch


def _session_factory_for(identity, bedrock_summaries):
    def factory(region_name=None):
        session = Mock()

        def client(service_name, region_name=None):
            c = Mock()
            if service_name == "sts":
                c.get_caller_identity.return_value = identity
            elif service_name == "bedrock":
                c.list_foundation_models.return_value = {"modelSummaries": bedrock_summaries}
                c.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
            elif service_name == "secretsmanager":
                c.describe_secret.side_effect = Exception("not found")
            elif service_name == "cloudformation":
                c.describe_stacks.side_effect = Exception("not found")
            return c

        session.client.side_effect = client
        return session

    return factory


def test_start_global_runs_identity_and_git_in_parallel(tmp_path):
    identity = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"}
    factory = _session_factory_for(identity, [])
    prefetcher = prefetch.Prefetcher(factory, tmp_path)
    prefetcher.start_global()
    result = prefetcher.identity_future.result(timeout=10)
    assert result.account_id == "123456789012"
    assert result.partition == "aws"
    info = prefetcher.git_future.result(timeout=10)
    assert info.branch  # tmp_path is not a git repo, but detect() must not raise
    visibility = prefetcher.github_visibility_future.result(timeout=10)
    assert visibility == "unknown"
    prefetcher.shutdown()


def test_start_region_caches_futures_per_region(tmp_path):
    identity = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"}
    factory = _session_factory_for(identity, [])
    prefetcher = prefetch.Prefetcher(factory, tmp_path)

    jobs_a = prefetcher.start_region("us-east-1")
    jobs_a_again = prefetcher.start_region("us-east-1")
    assert jobs_a is jobs_a_again

    jobs_b = prefetcher.start_region("us-west-2")
    assert jobs_b is not jobs_a
    assert prefetcher.region_jobs("us-east-1") is jobs_a
    assert prefetcher.region_jobs("us-west-2") is jobs_b
    assert prefetcher.region_jobs("eu-west-1") is None

    jobs_a.models.result(timeout=10)
    jobs_b.models.result(timeout=10)
    prefetcher.shutdown()


def test_region_jobs_resolve_without_error(tmp_path):
    identity = {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"}
    factory = _session_factory_for(identity, [])
    prefetcher = prefetch.Prefetcher(factory, tmp_path)
    jobs = prefetcher.start_region("us-east-1")

    discovery = jobs.models.result(timeout=10)
    assert discovery.all_items() == []
    assert jobs.pat_secret_arn.result(timeout=10) is None
    toolkit = jobs.cdk_toolkit.result(timeout=10)
    assert toolkit == {"us-east-1": False}
    assert jobs.spike_stacks.result(timeout=10) == []
    prefetcher.shutdown()
