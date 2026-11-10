# Development operations

This runbook describes the monitoring defined in the current source tree. It is
not evidence that a particular stack or alarm is deployed, that notification
delivery works, or that the product is release-ready. Check the deployed
CloudFormation stack, its commit, the region, and the CodeBuild result before
using it. Builds run in CodeBuild under B91; follow the approved deployment
method for the environment. The current iteration excludes file uploads and
managed RAG.

## Monitoring and starting thresholds

`backend/lib/constructs/observability.ts` defines an operations dashboard and
an SNS topic named `cei-life-sciences-alerts`. When an administrator email is
configured, SNS sends a confirmation email; the recipient must confirm the
subscription before alarm notifications can arrive. The following thresholds
are initial settings over five-minute periods, not validated production limits:

| Signal | Initial alarm threshold |
| --- | --- |
| API Gateway server errors | At least 5% of requests; zero when there is no traffic |
| Lambda errors and throttles | At least one per function |
| DynamoDB throttled requests | At least one per monitored table and operation group |
| Handled application failures | At least one matching configured handler log event |
| Cognito pre-sign-up attempts | At least 20 |
| Gateway policy deny log matches | At least five; see limitations below |
| CloudFront WAF blocked requests | At least 25% of at least 100 allowed or blocked requests |

The WAF alarm is defined in `EdgeWafStack`, in `us-east-1` for CloudFront.
It references the application region's SNS topic by ARN. Verify end-to-end
delivery in the actual region configuration rather than assuming that a
configured action delivered a message. Application metrics and the dashboard
are in the application stack's region. Thresholds should be reviewed against
real development traffic before any production rollout.

The dashboard includes browser failure counts and agent application errors;
neither has a separate alarm. B75 excludes stale-job and agent-health alarms:
the watchdog and live job view must handle those conditions. CDK defines the
watchdog (`JobWatchdog`, run every 5 minutes by an EventBridge rule in
`backend/lib/constructs/background-jobs.ts`). Its checkpoint resume is off
(`CEI_WATCHDOG_RESUME_ENABLED=false`). The watchdog and the full background
job workflow are not yet proven on a live deployment.

## Verification in an isolated development environment

1. Confirm the deployed stack versions and resources in CloudFormation and
   the tested commit in the CodeBuild result. Confirm the SNS email subscription
   is `Confirmed`; no subscription is added if the admin email is absent.
2. In CloudWatch, inspect the dashboard and each alarm's metric, dimensions,
   recent datapoints and action. In `us-east-1`, inspect the WAF alarm separately.
   `INSUFFICIENT_DATA` or an empty graph is not proof the signal works.
3. With synthetic accounts and a controlled, approved development test, cause a
   bounded API 5xx and verify that the metric, alarm state, SNS delivery and
   email agree. Return the test configuration to normal afterwards. Record the
   test window, deployed commit, alarm ARN and outcome without including secrets,
   prompts or user records.
4. Verify the remaining signals using controlled test events and traffic in the
   same way. For WAF, exercise the intended rule only in the isolated test
   environment and account for the 100-request minimum and five-minute window.
   Do not perform an unbounded traffic burst.
5. Test a permitted and a denied Gateway action using separate, scope-limited
   synthetic principals. Check the emitted Gateway event and ensure the policy
   denial filter counts the actual event before relying on its alarm.

## Known gaps

- The Gateway policy-denial alarm currently filters the log term `DENY`; its
  accuracy against the actual delivered event format and notification path has
  not been demonstrated. `LOG_ONLY` records decisions but does not enforce them;
  it cannot demonstrate cross-source isolation. Verify enforcement separately.
- The input PII Guardrail construct is not connected to production agent calls,
  and a Guardrail intervention alarm is not yet connected. Add and test that
  alarm when the Guardrail and its actual intervention metric are integrated.
- Alert thresholds, WAF cross-region delivery, and alarm firing have not been
  validated by a deployed user journey. Trace coverage and sensitive-data
  redaction also require the security review.
- The report storage and deletion helpers are not by themselves proof that
  export creation or chat/account deletion is connected end to end.

Avoid placing prompts, tokens, source records or user identifiers in alarm
descriptions or shared incident notes. Use the request ID and controlled access
to the relevant logs to investigate an incident.
