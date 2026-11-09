# Streaming integration validation handoff

The code-only work does not deploy resources. Before a sandbox deployment, review the branch and current AWS state. All builds and deployments use the repository's CodeBuild workflow (B91).

## Implementation still required

The Runner, Router, per-user leases, Gateway client, Research Memory resource, SSE persistence and frontend are wired in code. The Research streaming flag is enabled only when the configured Gateway policy mode is `ENFORCE`. None of that wiring has been verified by CodeBuild or in a deployed environment; run the validation below before using it.

Research requires a verified per-run Gateway token. The existing direct Research proxy does not supply one, so with the streaming flag off it cannot retrieve sources; do not deploy this branch expecting that fallback to work. Other specialists keep their previous source transport pending their own Gateway migrations. The F4 live test now accepts separately provisioned sandbox tokens in environment variables rather than assuming the Router role; an authorized sandbox token fixture and PatentsView credentials are still needed. Provider-wide rate-limit enforcement remains open; see `docs/source-rate-limit-review.md`.

F19 deletion must query `ActiveRunsStore` using `user_id = <verified user>` and `begins_with(record, 'replay#<session_id>#')`, follow each `history_partition` to delete all matching `ChatHistoryStore` rows, then delete each index row. Account deletion must cover all sessions and any remaining `replay#` rows. The Runner reserves the head and index atomically; neither row has a TTL.

1. Run the build and tests in a CodeBuild job using `buildspec.validation.yml`. The standard `buildspec.yml` ends with `cdk deploy --all`, so do not invoke it unchanged for a code-only check.
2. When sandbox deployment is authorized, use `./deploy.sh --spike streaming` and obtain the spike's `StreamUrl`, `UserPoolId`, and `UserPoolClientId` outputs. Provision a disposable test user and obtain an ID token using an approved secure workflow. Set `SPIKE_URL` and `SPIKE_ID_TOKEN` in the environment, then run `node backend/lambdas/spike-streaming-runner/test-client.mjs`. Avoid printing the token. Test missing and expired tokens separately.
3. Record the live S1 findings, including whether progressive delivery, timeouts and caching behave as expected. The script tests more than ten minutes, not the 15-minute limit or throughput cap.
4. Validate all Gateway source targets and source-specific `tools/list` filtering and allowed/denied calls in a sandbox under `ENFORCE`. Verify the CMS response contains real NCD records and citations. Confirm the Router is the only intended holder of the M2M secret, refresh rejects inactive/unauthorized runs, and one user's sessions cannot be accessed by another.
5. Exercise real authenticated Research through CloudFront: progressive events, grounding, trace redaction and retrieval, interruption/reconnect, concurrent-run limits, and requests above the long-run threshold. Verify direct source invocation cannot bypass the Gateway.
6. After S1/S2 validation, explicitly destroy their isolated stacks and remove their temporary hooks under B92. The S2 stack was recorded as retained; check its current state before any action.

F3's historical deployed check did not exercise an expired token. None of the checks above should be marked passed based solely on code or unit tests.
